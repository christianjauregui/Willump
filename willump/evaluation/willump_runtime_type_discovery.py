import ast
import copy
import numpy
from typing import List, Optional
import pandas.core.frame

from willump.evaluation.willump_graph_builder import WillumpGraphBuilder
from willump import *
import scipy.sparse.csr

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

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        new_node = copy.deepcopy(node)
        new_body: List[ast.stmt] = []
        for i, arg in enumerate(node.args.args):
            # First, type the arguments.
            argument_name: str = arg.arg
            argument_internal_name = "__willump_arg{0}".format(i)
            argument_instrumentation_code: str = \
                """willump_typing_map["{0}"] = py_var_to_weld_type({0})\n""" \
                    .format(argument_name) + \
                """willump_typing_map["{0}"] = py_var_to_weld_type({1})""" \
                    .format(argument_internal_name, argument_name)
            instrumentation_ast: ast.Module = ast.parse(argument_instrumentation_code, "exec")
            instrumentation_statements: List[ast.stmt] = instrumentation_ast.body
            new_body = new_body + instrumentation_statements
        for body_entry in node.body:
            if isinstance(body_entry, ast.Assign):
                # Type all variables as they are assigned.
                new_body.append(body_entry)
                assert (len(body_entry.targets) == 1)  # Assume assignment to only one variable.
                target: ast.expr = body_entry.targets[0]
                target_type_statement: List[ast.stmt] = self._analyze_target_type(target)
                new_body = new_body + target_type_statement
                # Remember static variables if present.
                value: ast.expr = body_entry.value
                extract_static_vars_statements = self._maybe_extract_static_variables(value)
                new_body = new_body + extract_static_vars_statements
            elif isinstance(body_entry, ast.Return):
                # Type the function's return value.
                new_assignment: ast.Assign = ast.Assign()
                new_assignment_target: ast.Name = ast.Name()
                new_assignment_target.id = "__willump_retval"
                new_assignment_target.ctx = ast.Store()
                new_assignment.targets = [new_assignment_target]
                new_assignment.value = body_entry.value
                new_body.append(new_assignment)
                return_type_statement: List[ast.stmt] = \
                    self._analyze_target_type(new_assignment_target)
                new_body = new_body + return_type_statement
                new_body.append(body_entry)
        new_node.body = new_body
        # No recursion allowed!
        new_node.decorator_list = []
        return ast.copy_location(new_node, node)

    @staticmethod
    def _maybe_extract_static_variables(value: ast.expr) -> List[ast.stmt]:
        return_statements: List[ast.stmt] = []
        if isinstance(value, ast.Call):
            called_function_name: str = WillumpGraphBuilder._get_function_name(value)
            if "willump_frequency_count" in called_function_name:
                vocab_dict_name: str = value.args[1].id
                static_variable_extraction_code = \
                    """willump_static_vars["{0}"] = {1}""" \
                        .format(WILLUMP_FREQUENCY_COUNT_VOCAB, vocab_dict_name)
                freq_count_instrumentation_ast: ast.Module = \
                    ast.parse(static_variable_extraction_code, "exec")
                freq_count_instrumentation_statements: List[ast.stmt] = \
                    freq_count_instrumentation_ast.body
                return_statements += freq_count_instrumentation_statements
            elif "predict" in called_function_name:
                static_variable_extraction_code = \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_LOGISTIC_REGRESSION_WEIGHTS, "model.coef_") + \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_LOGISTIC_REGRESSION_INTERCEPT, "model.intercept_")
                logit_instrumentation_ast: ast.Module = \
                    ast.parse(static_variable_extraction_code, "exec")
                logit_instrumentation_statements: List[ast.stmt] = logit_instrumentation_ast.body
                return_statements += logit_instrumentation_statements
            elif "transform" in called_function_name:
                static_variable_extraction_code = \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_COUNT_VECTORIZER_VOCAB, "input_vect.vocabulary_") + \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_COUNT_VECTORIZER_ANALYZER, "input_vect.analyzer") + \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_COUNT_VECTORIZER_NGRAM_RANGE, "input_vect.ngram_range") + \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_COUNT_VECTORIZER_LOWERCASE, "input_vect.lowercase")
                count_vectorizer_instrumentation_ast: ast.Module = \
                    ast.parse(static_variable_extraction_code, "exec")
                count_vectorizer_instrumentation_statements: List[ast.stmt] = count_vectorizer_instrumentation_ast.body
                return_statements += count_vectorizer_instrumentation_statements
            elif "merge" in called_function_name:
                # TODO:  More robust extraction.
                static_variable_extraction_code = \
                    """willump_static_vars["{0}"] = {1}\n""" \
                        .format(WILLUMP_JOIN_RIGHT_DATAFRAME + str(value.lineno), value.args[0].id) + \
                    """willump_static_vars["{0}"] = {1}.columns\n""" \
                        .format(WILLUMP_JOIN_LEFT_COLUMNS + str(value.lineno), value.func.value.id) + \
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

    @staticmethod
    def _analyze_target_type(target: ast.expr) -> List[ast.stmt]:
        """
        Create a statement from the target of an assignment that will insert into a global
        dict the type of the target.
        """
        target_name: str = WillumpGraphBuilder.get_assignment_target_name(target)
        target_analysis_instrumentation_code: str = \
            """willump_typing_map["{0}"] = py_var_to_weld_type({0})""".format(target_name)
        instrumentation_ast: ast.Module = ast.parse(target_analysis_instrumentation_code, "exec")
        instrumentation_statements: List[ast.stmt] = instrumentation_ast.body
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
    # TODO:  Find a way around this ridiculous special-casing for lists.
    # TODO:  This fails badly if a list (of strings) shows up empty (degenerate input).
    elif isinstance(py_var, list) and len(py_var) > 0 and isinstance(py_var[0], str):
        return WeldVec(WeldStr())
    # Sparse matrix type used by CountVectorizer
    elif isinstance(py_var, scipy.sparse.csr.csr_matrix):
        if py_var.dtype == numpy.int8:
            return WeldCSR(WeldChar())
        elif py_var.dtype == numpy.int16:
            return WeldCSR(WeldInt16())
        elif py_var.dtype == numpy.int32:
            return WeldCSR(WeldInt())
        elif py_var.dtype == numpy.int64:
            return WeldCSR(WeldLong())
        elif py_var.dtype == numpy.float32:
            return WeldCSR(WeldFloat())
        elif py_var.dtype == numpy.float64:
            return WeldCSR(WeldDouble())
        else:
            panic("Unrecognized ndarray type {0}".format(py_var.dtype.__str__()))
            return None
    # This type is a placeholder, during graph inference it will be replaced by the real type.
    elif isinstance(py_var, pandas.core.frame.DataFrame):
        return WeldType()
    # TODO:  Handle multidimensional arrays
    elif isinstance(py_var, numpy.ndarray):
        if py_var.dtype == numpy.int8:
            return WeldVec(WeldChar())
        elif py_var.dtype == numpy.int16:
            return WeldVec(WeldInt16())
        elif py_var.dtype == numpy.int32:
            return WeldVec(WeldInt())
        elif py_var.dtype == numpy.int64:
            return WeldVec(WeldLong())
        elif py_var.dtype == numpy.float32:
            return WeldVec(WeldFloat())
        elif py_var.dtype == numpy.float64:
            return WeldVec(WeldDouble())
        else:
            panic("Unrecognized ndarray type {0}".format(py_var.dtype.__str__()))
            return None
    else:
        # print("Unrecognized var type {0}".format(type(py_var)))
        return None
