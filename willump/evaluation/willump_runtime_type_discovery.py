import ast
import copy
import numpy
from typing import List, Optional
import pandas.core.frame

from willump.evaluation.willump_graph_builder import WillumpGraphBuilder
from willump import *
import scipy.sparse.csr
from willump.willump_utilities import *

from weld.types import *


class WillumpRuntimeTypeDiscovery(ast.NodeTransformer):
    """
    Annotate the AST of a Python function to record the Weld type of every variable after it is
    assigned. This must run in a global namespace that contains a willump_typing_map variable
    (into which the types will be recorded) as well as the py_var_to_weld_type function.

    Also extract a list of values of "static variables"--important and unchanging values such as
    the weights of a logistic regression model or the contents of a vocabulary.

    TODO:  Add support for control flow changes inside the function body.
    """

    def process_body(self, body):
        new_body: List[ast.stmt] = []
        for body_entry in body:
            if isinstance(body_entry, ast.Assign):
                # Timing start code
                timing_start_code: str = \
                    """t0 = time.time()\n"""
                timing_start_ast: ast.Module = ast.parse(timing_start_code, "exec")
                timing_start_statement: List[ast.stmt] = timing_start_ast.body
                new_body = new_body + timing_start_statement
                # Type all variables as they are assigned.
                new_body.append(body_entry)
                # Timing end code
                timing_end_code: str = \
                    """willump_timing_map[%d] = time.time() - t0\n""" % body_entry.lineno
                timing_end_ast: ast.Module = ast.parse(timing_end_code, "exec")
                timing_end_statement: List[ast.stmt] = timing_end_ast.body
                new_body = new_body + timing_end_statement
                # Typing code
                assert (len(body_entry.targets) == 1)  # Assume assignment to only one variable.
                target: ast.expr = body_entry.targets[0]
                target_type_statement: List[ast.stmt] = self._analyze_target_type(target)
                new_body = new_body + target_type_statement
                # Remember static variables if present.
                value: ast.expr = body_entry.value
                extract_static_vars_statements = self._maybe_extract_static_variables(value)
                new_body = new_body + extract_static_vars_statements
            else:
                new_body.append(body_entry)
        return new_body

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        new_node = copy.deepcopy(node)
        self.generic_visit(new_node)
        new_body: List[ast.stmt] = []
        for i, arg in enumerate(new_node.args.args):
            # First, type the arguments.
            argument_name: str = arg.arg
            argument_instrumentation_code: str = \
                """willump_typing_map["{0}_{1}"] = py_var_to_weld_type({0})\n""" \
                    .format(argument_name, node.lineno)
            instrumentation_ast: ast.Module = ast.parse(argument_instrumentation_code, "exec")
            instrumentation_statements: List[ast.stmt] = instrumentation_ast.body
            new_body = new_body + instrumentation_statements
        new_body += self.process_body(new_node.body)
        new_node.body = new_body
        # No recursion allowed!
        new_node.decorator_list = []
        return ast.copy_location(new_node, node)

    def visit_For(self, node: ast.For):
        new_node = copy.deepcopy(node)
        self.generic_visit(new_node)
        new_node.body = self.process_body(new_node.body)
        new_node.orelse = self.process_body(new_node.orelse)
        return ast.copy_location(new_node, node)

    def visit_If(self, node: ast.If):
        new_node = copy.deepcopy(node)
        self.generic_visit(new_node)
        new_node.body = self.process_body(new_node.body)
        new_node.orelse = self.process_body(new_node.orelse)
        return ast.copy_location(new_node, node)

    def visit_While(self, node: ast.While):
        new_node = copy.deepcopy(node)
        self.generic_visit(new_node)
        new_node.body = self.process_body(new_node.body)
        new_node.orelse = self.process_body(new_node.orelse)
        return ast.copy_location(new_node, node)

    def visit_With(self, node: ast.With):
        new_node = copy.deepcopy(node)
        self.generic_visit(new_node)
        new_node.body = self.process_body(new_node.body)
        return ast.copy_location(new_node, node)

    @staticmethod
    def _maybe_extract_static_variables(value: ast.expr) -> List[ast.stmt]:
        return_statements: List[ast.stmt] = []
        if isinstance(value, ast.Subscript):
            if isinstance(value.slice, ast.Index) and isinstance(value.slice.value, ast.Name):
                index_name = value.slice.value.id
                static_variable_extraction_code = \
                    """willump_static_vars["{0}"] = {1}""" \
                        .format(WILLUMP_SUBSCRIPT_INDEX_NAME + str(value.lineno), index_name)
                index_name_instrumentation_ast: ast.Module = \
                    ast.parse(static_variable_extraction_code, "exec")
                index_name_instrumentation_statements: List[ast.stmt] = \
                    index_name_instrumentation_ast.body
                return_statements += index_name_instrumentation_statements
        elif isinstance(value, ast.Call):
            called_function_name: str = WillumpGraphBuilder._get_function_name(value)
            if called_function_name is None:
                pass
            elif "willump_train_function" in called_function_name:
                x_name = value.args[0].id
                y_name = value.args[1].id
                model_x_y_variable_extraction_code = "willump_static_vars['%s'] = (%s, %s)" % \
                                                     (WILLUMP_TRAIN_X_Y, x_name, y_name)
                model_x_y_variable_extraction_ast: ast.Module = ast.parse(model_x_y_variable_extraction_code, "exec")
                model_x_y_variable_extraction_statements: List[ast.stmt] = model_x_y_variable_extraction_ast.body
                return_statements += model_x_y_variable_extraction_statements
            elif "willump_predict_function" \
                    in called_function_name or "willump_predict_proba_function" in called_function_name:
                x_name = value.args[1].id
                train_variable_extraction_code = "willump_static_vars['%s'] = %s.shape[1]" % (WILLUMP_INPUT_WIDTH, x_name)
                train_variable_extraction_ast: ast.Module = ast.parse(train_variable_extraction_code, "exec")
                train_variable_extraction_statements: List[ast.stmt] = train_variable_extraction_ast.body
                return_statements += train_variable_extraction_statements
            elif ".transform" in called_function_name:
                if isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Name):
                    lineno = str(value.lineno)
                    transformer_name = value.func.value.id
                    static_variable_extraction_code = \
                        """willump_static_vars["{0}"] = {1}.{2}\n""" \
                            .format(WILLUMP_COUNT_VECTORIZER_VOCAB + lineno, transformer_name, "vocabulary_") + \
                        """if type({0}).__name__ == "TfidfVectorizer" or type({0}).__name__ == "CountVectorizer":\n""".format(
                            transformer_name) + \
                        """\twillump_static_vars["{0}"] = {1}.{2}\n""" \
                            .format(WILLUMP_COUNT_VECTORIZER_ANALYZER + lineno, transformer_name, "analyzer") + \
                        """if type({0}).__name__ == "TfidfVectorizer" or type({0}).__name__ == "CountVectorizer":\n""".format(
                            transformer_name) + \
                        """\twillump_static_vars["{0}"] = {1}.{2}\n""" \
                            .format(WILLUMP_COUNT_VECTORIZER_NGRAM_RANGE + lineno, transformer_name, "ngram_range") + \
                        """if type({0}).__name__ == "TfidfVectorizer" or type({0}).__name__ == "CountVectorizer":\n""".format(
                            transformer_name) + \
                        """\twillump_static_vars["{0}"] = {1}.{2}\n""" \
                            .format(WILLUMP_COUNT_VECTORIZER_LOWERCASE + lineno, transformer_name, "lowercase") + \
                        """if type({0}).__name__ == "TfidfVectorizer":\n""".format(transformer_name) + \
                        """\twillump_static_vars["{0}"] = {1}.{2}\n""" \
                            .format(WILLUMP_TFIDF_IDF_VECTOR + lineno, transformer_name, "idf_")
                    count_vectorizer_instrumentation_ast: ast.Module = \
                        ast.parse(static_variable_extraction_code, "exec")
                    count_vectorizer_instrumentation_statements: List[
                        ast.stmt] = count_vectorizer_instrumentation_ast.body
                    return_statements += count_vectorizer_instrumentation_statements
            elif ".merge" in called_function_name:
                # TODO:  More robust extraction.
                static_variable_extraction_code = \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_JOIN_RIGHT_DATAFRAME + str(value.lineno), value.args[0].id) + \
                    """willump_static_vars["{0}"] = {1}.dtypes\n""" \
                        .format(WILLUMP_JOIN_LEFT_DTYPES + str(value.lineno), value.func.value.id) + \
                    """willump_static_vars["{0}"] = "{1}"\n""" \
                        .format(WILLUMP_JOIN_HOW + str(value.lineno), value.keywords[0].value.s)
                if isinstance(value.keywords[1].value, ast.Name):
                    static_variable_extraction_code += \
                        """willump_static_vars["{0}"] = {1}\n""" \
                            .format(WILLUMP_JOIN_COL + str(value.lineno), value.keywords[1].value.id)
                else:
                    static_variable_extraction_code += \
                        """willump_static_vars["{0}"] = "{1}"\n""" \
                            .format(WILLUMP_JOIN_COL + str(value.lineno), value.keywords[1].value.s)
                join_instrumentation_ast: ast.Module = \
                    ast.parse(static_variable_extraction_code, "exec")
                join_instrumentation_statements: List[ast.stmt] = join_instrumentation_ast.body
                return_statements += join_instrumentation_statements

        return return_statements

    def _analyze_target_type(self, target: ast.expr) -> List[ast.stmt]:
        """
        Create a statement from the target of an assignment that will insert into a global
        dict the type of the target.
        """
        instrumentation_statements: List[ast.stmt] = []
        if isinstance(target, ast.Tuple):
            for target in target.elts:
                target_name: str = WillumpGraphBuilder.get_assignment_target_name(target)
                target_analysis_instrumentation_code: str = \
                    """willump_typing_map["{0}_{1}"] = py_var_to_weld_type({0})""".format(target_name, target.lineno)
                instrumentation_ast: ast.Module = ast.parse(target_analysis_instrumentation_code, "exec")
                instrumentation_statements += instrumentation_ast.body
        else:
            target_name: str = WillumpGraphBuilder.get_assignment_target_name(target)
            target_analysis_instrumentation_code: str = \
                """willump_typing_map["{0}_{1}"] = py_var_to_weld_type({0})""".format(target_name, target.lineno)
            instrumentation_ast: ast.Module = ast.parse(target_analysis_instrumentation_code, "exec")
            instrumentation_statements += instrumentation_ast.body
        return instrumentation_statements


def py_var_to_weld_type(py_var: object) -> Optional[WeldType]:
    """
    Get the Weld type of a Python variable.

    TODO:  Handle more types of variables.
    """
    if isinstance(py_var, int):
        return WeldLong()
    elif isinstance(py_var, float):
        return WeldDouble()
    elif isinstance(py_var, str):
        return WeldStr()
    # TODO:  Find a more robust way to handle list types, this fails badly if the input is degenerate.
    elif isinstance(py_var, list) and len(py_var) > 0 and isinstance(py_var[0], str):
        return WeldVec(WeldStr())
    # Sparse matrix type used by CountVectorizer
    elif isinstance(py_var, scipy.sparse.csr.csr_matrix):
        if py_var.dtype == numpy.int8:
            return WeldCSR(WeldChar(), width=py_var.shape[1])
        elif py_var.dtype == numpy.int16:
            return WeldCSR(WeldInt16(), width=py_var.shape[1])
        elif py_var.dtype == numpy.int32:
            return WeldCSR(WeldInt(), width=py_var.shape[1])
        elif py_var.dtype == numpy.int64:
            return WeldCSR(WeldLong(), width=py_var.shape[1])
        elif py_var.dtype == numpy.float32:
            return WeldCSR(WeldFloat(), width=py_var.shape[1])
        elif py_var.dtype == numpy.float64:
            return WeldCSR(WeldDouble(), width=py_var.shape[1])
        else:
            panic("Unrecognized ndarray type {0}".format(py_var.dtype.__str__()))
            return None
    elif isinstance(py_var, pandas.core.frame.DataFrame):
        df_col_weld_types = []
        for dtype in py_var.dtypes:
            col_weld_type: WeldType = numpy_type_to_weld_type(dtype)
            df_col_weld_types.append(WeldVec(col_weld_type))
        return WeldPandas(df_col_weld_types, list(py_var.columns))
    elif isinstance(py_var, pandas.core.series.Series):
        weld_elem_type: WeldType = numpy_type_to_weld_type(py_var.dtype)
        return WeldSeriesPandas(weld_elem_type, list(py_var.index))
    elif isinstance(py_var, numpy.ndarray):
        if py_var.ndim > 1:
            return WeldVec(py_var_to_weld_type(py_var[0]), width=py_var.shape[1])
        if py_var.dtype == numpy.int8:
            return WeldVec(WeldChar())
        elif py_var.dtype == numpy.uint8:
            return WeldVec(WeldUnsignedChar())
        elif py_var.dtype == numpy.int16:
            return WeldVec(WeldInt16())
        elif py_var.dtype == numpy.uint16:
            return WeldVec(WeldUnsignedInt16())
        elif py_var.dtype == numpy.int32:
            return WeldVec(WeldInt())
        elif py_var.dtype == numpy.uint32:
            return WeldVec(WeldUnsignedInt())
        elif py_var.dtype == numpy.int64:
            return WeldVec(WeldLong())
        elif py_var.dtype == numpy.uint64:
            return WeldVec(WeldUnsignedLong())
        elif py_var.dtype == numpy.float32:
            return WeldVec(WeldFloat())
        elif py_var.dtype == numpy.float64:
            return WeldVec(WeldDouble())
        elif py_var.dtype == numpy.object:
            return WeldVec(py_var_to_weld_type(py_var[0]))
        else:
            panic("Unrecognized ndarray type {0}".format(py_var.dtype.__str__()))
            return None
    else:
        # print("Unrecognized var type {0}".format(type(py_var)))
        return None
