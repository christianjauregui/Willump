import ast

from willump import panic
from willump.graph.willump_graph import WillumpGraph
from willump.graph.willump_graph_node import WillumpGraphNode
from willump.graph.willump_input_node import WillumpInputNode
from willump.graph.willump_output_node import WillumpOutputNode
from willump.graph.transform_math_node import TransformMathNode, MathOperation, MathOperationInput
from willump.graph.string_split_node import StringSplitNode
from willump.graph.string_lower_node import StringLowerNode
from willump.graph.string_removechar_node import StringRemoveCharNode
from willump.graph.vocabulary_frequency_count_node import VocabularyFrequencyCountNode
from willump.graph.logistic_regression_node import LogisticRegressionNode
from willump.graph.array_append_node import ArrayAppendNode
from willump.graph.willump_python_node import WillumpPythonNode

from typing import MutableMapping, List, Tuple, Optional, Set, Mapping
import typing
from weld.types import *


class WillumpGraphBuilder(ast.NodeVisitor):
    """
    Builds a Willump graph from the Python AST for a FunctionDef.  Typically called from a
    decorator around that function.  Makes the following assumptions:

    1.  Input Python is the definition of a single function,
        from which the graph shall be extracted.

    2.  The function does not reference anything outside of its own scope.

    TODO:  Implement UDFs to weaken assumption 2 as well as deal with syntax we don't recognize.
    """
    willump_graph: WillumpGraph
    # A list of all argument names in the order they are passed in.
    arg_list: List[str]
    # A map from the names of variables to the nodes that generate them.
    _node_dict: MutableMapping[str, WillumpGraphNode]
    # A list of all mathops found.
    _mathops_list: List[MathOperation]
    # A map from variables to their Weld types.
    _type_map: MutableMapping[str, WeldType]
    # A set of static variable values saved from Python execution.
    _static_vars: Mapping[str, object]
    # Saved data structures required by some nodes.
    aux_data: List[Tuple[int, WeldType]]

    def __init__(self, type_map: MutableMapping[str, WeldType],
                 static_vars: Mapping[str, object]) -> None:
        self._node_dict = {}
        self._mathops_list = []
        self._type_map = type_map
        self._static_vars = static_vars
        self.arg_list = []
        self.aux_data = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """
        Begin processing of a function.  Create input nodes for function arguments.
        """
        for arg in node.args.args:
            arg_name: str = arg.arg
            input_node: WillumpInputNode = WillumpInputNode(arg_name)
            self._node_dict[arg_name] = input_node
            self.arg_list.append(arg_name)
        for entry in node.body:
            if isinstance(entry, ast.Assign):
                result = self.analyze_Assign(entry)
                if result is None:
                    entry_analyzer = ExpressionVariableAnalyzer()
                    entry_analyzer.visit(entry)
                    input_list, output_list = entry_analyzer.get_in_out_list()
                    # TODO:  Multiple outputs.
                    assert(len(output_list) == 1)
                    output_var_name = output_list[0]
                    input_node_list = []
                    for input_var in input_list:
                        if input_var in self._node_dict:
                            input_node_list.append(self._node_dict[input_var])
                    willump_python_node: WillumpPythonNode = WillumpPythonNode(entry, output_var_name, input_node_list)
                    self._node_dict[output_var_name] = willump_python_node
                else:
                    output_var_name, node_or_mathop = result
                    if node_or_mathop is None:
                        pass
                    elif isinstance(node_or_mathop, WillumpGraphNode):
                        self._node_dict[output_var_name] = node_or_mathop
                    else:
                        self._mathops_list.append(node_or_mathop)
            elif isinstance(entry, ast.Return):
                self.analyze_Return(entry)
            elif isinstance(entry, ast.For):
                for for_entry in entry.body:
                    if isinstance(for_entry, ast.Assign):
                        result = self.analyze_Assign(for_entry)
                        if result is None:
                            panic("Unrecognized assign %s" % ast.dump(for_entry))
                        else:
                            output_var_name, node_or_mathop = result
                            if node_or_mathop is None:
                                pass
                            elif isinstance(node_or_mathop, WillumpGraphNode):
                                self._node_dict[output_var_name] = node_or_mathop
                            else:
                                self._mathops_list.append(node_or_mathop)
            else:
                panic("Unrecognized body node %s" % ast.dump(entry))

    def analyze_Assign(self, node: ast.Assign) -> \
            Optional[Tuple[str, Optional[typing.Union[WillumpGraphNode, MathOperation]]]]:
        """
        Process an assignment AST node into either a Willump node or MathOperation that
        defines the variable being assigned.
        """
        assert(len(node.targets) == 1)  # Assume assignment to only one variable.
        target: ast.expr = node.targets[0]
        output_var_name = self.get_assignment_target_name(target)
        if output_var_name is None:
            return None
        output_type = self._type_map[output_var_name]
        value: ast.expr = node.value
        if isinstance(value, ast.BinOp):
            analyzed_binop = self._analyze_binop(value)
            if analyzed_binop is None:
                return None
            else:
                left, op, right = analyzed_binop
            binop_mathop: MathOperation = MathOperation(op, output_var_name, output_type,
                                                        left, second_input=right)
            return output_var_name, binop_mathop
        elif isinstance(value, ast.Call):
            called_function: Optional[str] = self._get_function_name(value)
            if called_function is None:
                return None
            # Process unary math operations into MathOperations
            if self._pyunop_to_wunop(called_function) is not None:
                assert(len(value.args) == 1)  # Assume unary operation.
                operator: Optional[str] = self._pyunop_to_wunop(called_function)
                unary_input: Optional[MathOperationInput] = self._expr_to_math_operation_input(value.args[0])
                if operator is None or unary_input is None:
                    return None
                unary_mathop: MathOperation = MathOperation(operator, output_var_name,
                                                            output_type, unary_input)
                return output_var_name, unary_mathop
            # TODO:  Recognize functions in attributes properly.
            elif "split" in called_function:
                split_input_var: str = value.func.value.id
                split_input_node: WillumpGraphNode = self._node_dict[split_input_var]
                string_split_node: StringSplitNode = StringSplitNode(input_node=split_input_node,
                                                                     output_name=output_var_name)
                return output_var_name, string_split_node
            # TODO:  Recognize when this is being called in a loop, don't just assume it is.
            elif "lower" in called_function:
                lower_input_var: str = value.func.value.value.id
                lower_input_node: WillumpGraphNode = self._node_dict[lower_input_var]
                string_lower_node: StringLowerNode = StringLowerNode(input_node=lower_input_node,
                                                                     output_name=output_var_name)
                return output_var_name, string_lower_node
            # TODO:  Recognize replaces that do more than remove one character.
            elif "replace" in called_function:
                replace_input_var: str = value.func.value.value.id
                replace_input_node: WillumpGraphNode = self._node_dict[replace_input_var]
                target_char: str = value.args[0].s
                assert(len(target_char) == 1)
                assert(len(value.args[1].s) == 0)
                string_remove_char_node: StringRemoveCharNode =\
                    StringRemoveCharNode(input_node=replace_input_node,
                    target_char=target_char, output_name=output_var_name)
                return output_var_name, string_remove_char_node
            # TODO:  Find a real function to use here.
            elif "willump_frequency_count" in called_function:
                freq_count_input_var: str = value.args[0].id
                vocab_dict = self._static_vars["willump_frequency_count_vocab"]
                freq_count_input_node: WillumpGraphNode = self._node_dict[freq_count_input_var]
                vocab_freq_count_node: VocabularyFrequencyCountNode = VocabularyFrequencyCountNode(
                    input_node=freq_count_input_node, output_name=output_var_name,
                    input_vocab_dict=vocab_dict, aux_data=self.aux_data
                )
                return output_var_name, vocab_freq_count_node
            # TODO:  Lots of potential predictors, differentiate them!
            elif "predict" in called_function:
                logit_input_var: str = value.args[0].id
                logit_weights = self._static_vars["willump_logistic_regression_weights"]
                logit_intercept = self._static_vars["willump_logistic_regression_intercept"]
                logit_input_node: WillumpGraphNode = self._node_dict[logit_input_var]
                logit_node: LogisticRegressionNode = LogisticRegressionNode(
                    input_node=logit_input_node, output_name=output_var_name,
                    logit_weights=logit_weights,
                    logit_intercept=logit_intercept, aux_data=self.aux_data
                )
                return output_var_name, logit_node
            # TODO:  Support values that are not scalar variables.
            elif "numpy.append" in called_function:
                append_input_array: str = value.args[0].id
                append_input_value: str = value.args[1].id
                append_input_array_node: WillumpGraphNode = self._node_dict[append_input_array]
                append_input_val_node: WillumpGraphNode = self._node_dict[append_input_value]
                array_append_node: ArrayAppendNode = ArrayAppendNode(append_input_array_node, append_input_val_node,
                                                                output_var_name, self._type_map[append_input_array],
                                                                self._type_map[output_var_name])
                return output_var_name, array_append_node
            # TODO:  What to do with these?
            elif called_function == "numpy.zeros":
                return output_var_name, None
            elif "reshape" in called_function:
                return output_var_name, None
            else:
                return None
        else:
            return None

    def analyze_Return(self, node: ast.Return) -> None:
        """
        Process the function return and create a graph which outputs whatever the function
        is returning.

        Assumes function returns a single value, which must be a numpy float64 array.
        """
        output_node: WillumpOutputNode
        if isinstance(node.value, ast.Name):
            output_name: str = node.value.id
            if output_name in self._node_dict:
                output_node = WillumpOutputNode(WillumpPythonNode(node, output_name, [self._node_dict[output_name]]))
            else:
                potential_in_node: Optional[TransformMathNode] = \
                    self._build_math_transform_for_output(output_name)
                if potential_in_node is not None:
                    output_node = WillumpOutputNode(WillumpPythonNode(node, output_name, [potential_in_node]))
                else:
                    panic("No in-node found for return node {0}".format(ast.dump(node)))
            self.willump_graph = WillumpGraph(output_node)
        else:
            panic("Unrecognized return: {0}".format(ast.dump(node)))

    def get_willump_graph(self) -> WillumpGraph:
        return self.willump_graph

    def get_args_list(self) -> List[str]:
        return self.arg_list

    def get_aux_data(self) -> List[Tuple[int, WeldType]]:
        return self.aux_data

    def _expr_to_math_operation_input(self, expr: ast.expr) -> Optional[MathOperationInput]:
        """
        Convert an expression input to a binary or unary operation into a MathOperationInput.

        TODO:  Proper Subscript handling.

        TODO:  Stop turning all unknown types (say, subexpressions) into doubles.
        """
        if isinstance(expr, ast.Num):
            if isinstance(expr.n, float):
                return MathOperationInput(WeldDouble(), input_literal=expr.n)
            else:
                return MathOperationInput(WeldInt(), input_literal=expr.n)
        elif isinstance(expr, ast.Subscript):
            var_name: Optional[str] = self.get_assignment_target_name(expr)
            if var_name is None:
                return None
            return MathOperationInput(self._type_map[var_name],
                                      input_index=(var_name, expr.slice.value.n))
        elif isinstance(expr, ast.Name):
            return MathOperationInput(self._type_map[expr.id], input_var=expr.id)
        else:
            temp_target: ast.Name = ast.Name()
            temp_target.id = self._get_tmp_var_name()
            self._type_map[temp_target.id] = WeldDouble()
            temp_target.ctx = ast.Store()
            fake_assign: ast.Assign = ast.Assign()
            fake_assign.targets = [temp_target]
            fake_assign.value = expr
            result = self.analyze_Assign(fake_assign)
            if result is None:
                return None
            else:
                _, mathop = result
                self._mathops_list.append(mathop)
                return MathOperationInput(WeldDouble(), input_var=temp_target.id)

    def _analyze_binop(self, binop: ast.BinOp) -> \
            Optional[Tuple[MathOperationInput, str, MathOperationInput]]:
        """
        Convert an AST binop into two MathOperationInputs and an operator-string for a Transform
        Math Node.
        """
        operator: Optional[str] = self._pybinop_to_wbinop(binop.op)
        left_expr: ast.expr = binop.left
        left_input: Optional[MathOperationInput] = self._expr_to_math_operation_input(left_expr)
        right_expr: ast.expr = binop.right
        right_input: Optional[MathOperationInput] = self._expr_to_math_operation_input(right_expr)
        if left_input is None or right_input is None or operator is None:
            return None
        else:
            return left_input, operator, right_input

    def _build_math_transform_for_output(self, output_name: str) -> Optional[TransformMathNode]:
        """
        Build a TransformMathNode that will output a particular variable.  Return None
        if no such node can be built.

        TODO:  Don't recalculate intermediate variables.
        """
        assert(output_name not in self._node_dict)
        # All variables needed to compute the final output.
        input_vars: Set[str] = set()
        # All arrays needed to compute the final output.
        input_arrays: Set[str] = set()
        # All MathOperations in the node that computes the final output
        node_mathop_list: List[MathOperation] = []
        # Find all mathops that compute the output array as well as all their input dependencies.
        for mathop in reversed(self._mathops_list):
            mathop_output: str = mathop.output_var_name
            if mathop_output == output_name or mathop_output in input_vars:
                node_mathop_list.append(mathop)
                if mathop.first_input_var is not None:
                    input_vars.add(mathop.first_input_var)
                if mathop.first_input_index is not None:
                    input_arrays.add(mathop.first_input_index[0])
                if mathop.second_input_var is not None:
                    input_vars.add(mathop.second_input_var)
                if mathop.second_input_index is not None:
                    input_arrays.add(mathop.second_input_index[0])
            elif mathop_output in input_arrays:
                if mathop_output not in self._node_dict:
                    node: Optional[TransformMathNode] =\
                        self._build_math_transform_for_output(mathop_output)
                    if node is None:
                        return None
        # Return None if the final output is not buildable.
        if len(node_mathop_list) == 0:
            return None
        # The list was built backwards, reverse it.
        node_mathop_list.reverse()
        # Build the final node to return.
        input_nodes: List[WillumpGraphNode] =\
            list(map(lambda array: self._node_dict[array], input_arrays))
        return_node: TransformMathNode = TransformMathNode(input_nodes,
                                     node_mathop_list, output_name, self._type_map[output_name])
        self._node_dict[output_name] = return_node
        return return_node

    _temp_var_counter = 0

    def _get_tmp_var_name(self) -> str:
        _temp_var_name = "__graph_temp" + str(self._temp_var_counter)
        self._temp_var_counter += 1
        return _temp_var_name

    @staticmethod
    def get_assignment_target_name(node: ast.expr) -> Optional[str]:
        """
        Return the name of the target of an assignment statement.
        """
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Subscript):
            return WillumpGraphBuilder.get_assignment_target_name(node.value)
        else:
            return None

    @staticmethod
    def _get_function_name(call: ast.Call) -> Optional[str]:
        """
        Get the name of a function being called from a Call node.

        TODO:  Handle the ways different import statements can affect names. (e.g. numpy vs np).
        """
        def _get_layer_name(func) -> Optional[str]:
            if isinstance(func, ast.Name):
                return func.id
            elif isinstance(func, ast.Attribute):
                next_name = _get_layer_name(func.value)
                if next_name is None:
                    return None
                else:
                    return next_name + "." + func.attr
            elif isinstance(func, ast.Subscript):
                return _get_layer_name(func.value)
            else:
                return None
        return _get_layer_name(call.func)

    @staticmethod
    def _pybinop_to_wbinop(binop: ast.operator) -> Optional[str]:
        """
        Convert from AST binops to strings for TransformMathNode.
        """
        if isinstance(binop, ast.Add):
            return "+"
        elif isinstance(binop, ast.Sub):
            return "-"
        elif isinstance(binop, ast.Mult):
            return "*"
        elif isinstance(binop, ast.Div):
            return "/"
        else:
            return None

    @staticmethod
    def _pyunop_to_wunop(unop: str) -> Optional[str]:
        """
        Convert from Python function unops to strings for TransformMathModule.
        """
        if unop == "math.sqrt":
            return "sqrt"
        elif unop == "math.log":
            return "log"
        else:
            return None


class ExpressionVariableAnalyzer(ast.NodeVisitor):
    def __init__(self) -> None:
        self._output_list: List[str] = []
        self._input_list: List[str] = []

    def visit_Name(self, node: ast.Name):
        if isinstance(node.ctx, ast.Store):
            self._output_list.append(node.id)
        else:
            self._input_list.append(node.id)

    def get_in_out_list(self) -> Tuple[List[str], List[str]]:
        return self._input_list, self._output_list
