from willump.graph.willump_graph_node import WillumpGraphNode

from weld.types import *

from typing import List


class CombineLinearRegressionNode(WillumpGraphNode):
    """
    Combining Linear Regression Node.  Linear regression nodes and pushed-down linear regressions return vectors of
    floats from multiplying data by weights. This node sums those vectors, adds the intercept, and does classification.

    TODO:  Allow multi-class classification, not just binary.
    """
    _input_nodes: List[WillumpGraphNode]
    _output_name: str
    _output_type: WeldType
    _intercept_data_name: str

    def __init__(self, input_nodes: List[WillumpGraphNode], output_name: str,
                 intercept_data_name, output_type: WeldType) -> None:
        """
        Initialize the node.
        """
        self._output_name = output_name
        self._intercept_data_name = intercept_data_name
        self._input_nodes = input_nodes
        self._output_type = output_type

    def get_in_nodes(self) -> List[WillumpGraphNode]:
        return self._input_nodes

    def get_node_type(self) -> str:
        return "combine_logistic_regression"

    def get_node_weld(self) -> str:
        assert(isinstance(self._output_type, WeldVec))
        output_elem_type_str = str(self._output_type.elemType)
        zip_string: str = ""
        for input_node in self._input_nodes:
            zip_string += "%s," % input_node.get_output_name()
        zip_string = zip_string[:-1]
        if len(self._input_nodes) > 1:
            sum_string: str = ""
            for i in range(len(self._input_nodes)):
                sum_string += "x.$%d +" % i
            sum_string = sum_string[:-1]
        else:
            sum_string = "x"
        weld_program = \
            """
            let intercept: f64 = lookup(INTERCEPT_NAME, 0L);
            let OUTPUT_NAME: vec[OUTPUT_TYPE] = result(for(zip(ZIP_STRING),
                appender[OUTPUT_TYPE],
                | bs: appender[i64], i: i64, x |
                merge(bs, select(SUM_STRING + intercept > 0.0, OUTPUT_TYPE(1), OUTPUT_TYPE(0)))
            ));
            """
        weld_program = weld_program.replace("ZIP_STRING", zip_string)
        weld_program = weld_program.replace("SUM_STRING", sum_string)
        weld_program = weld_program.replace("INTERCEPT_NAME", self._intercept_data_name)
        weld_program = weld_program.replace("OUTPUT_NAME", self._output_name)
        weld_program = weld_program.replace("OUTPUT_TYPE", output_elem_type_str)
        return weld_program

    def get_output_name(self) -> str:
        return self._output_name

    def __repr__(self):
        return "Combine linear regression node for input {0} output {1}\n"\
            .format(str(self._input_nodes), self._output_name)
