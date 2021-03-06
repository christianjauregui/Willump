from willump.graph.willump_graph_node import WillumpGraphNode
from willump.graph.willump_input_node import WillumpInputNode
from willump.willump_utilities import *

from weld.types import *

from typing import List, Tuple, Mapping, Optional
import importlib


class WillumpHashJoinNode(WillumpGraphNode):
    """
    Implements a left join between two dataframes on several columns.  Fills missing rows with zeros.
    """
    left_input_name: str
    left_df_type: WeldPandas
    right_df_row_type: WeldPandas
    right_df_type: WeldPandas
    join_col_names: List[str]

    # Protected Cascade Variables
    _small_model_output_name: Optional[str] = None

    def __init__(self, input_node: WillumpGraphNode, input_name: str, left_input_type: WeldType, output_name: str, join_col_names: List[str],
                 right_dataframe, aux_data: List[Tuple[int, WeldType]]) -> None:
        """
        Initialize the node, appending a new entry to aux_data in the process.
        """
        self.left_input_name = input_name
        self._output_name = output_name
        self._right_dataframe = right_dataframe
        self._right_dataframe_name = "AUX_DATA_{0}".format(len(aux_data))
        self.join_col_names = join_col_names
        self._input_nodes = [input_node, WillumpInputNode(self._right_dataframe_name)]
        self._input_names = [input_name, self._right_dataframe_name]
        assert(isinstance(left_input_type, WeldPandas))
        self.left_df_type = left_input_type
        for entry in self._process_aux_data(right_dataframe):
            aux_data.append(entry)
        self._output_type = WeldPandas(field_types=self.left_df_type.field_types + self.right_df_type.field_types,
                                       column_names=self.left_df_type.column_names + self.right_df_type.column_names)

    def get_cost(self):
        # TODO:  Get a better idea of the relationship between right dataframe size and join cost.
        return 0.1

    def get_in_nodes(self) -> List[WillumpGraphNode]:
        return self._input_nodes

    def get_in_names(self) -> List[str]:
        return self._input_names

    def _process_aux_data(self, right_dataframe) -> List[Tuple[int, WeldType]]:
        """
        Returns a pointer to a Weld dict[join_col_type, {vec...vec}] of all the columns in right_dataframe
        indexed by join_col_name.
        """
        import willump.evaluation.willump_executor as wexec

        join_col_field_types = []
        join_col_type_map = {}
        join_col_lookup_string = ""
        for column, col_type in zip(self.left_df_type.column_names, self.left_df_type.field_types):
            if column in self.join_col_names:
                assert(isinstance(col_type, WeldVec))
                col_type = col_type.elemType
                join_col_field_types.append(col_type)
                join_col_type_map[column] = col_type
                col_right_df_index = list(right_dataframe.columns).index(column)
                join_col_lookup_string += "%s(lookup(_inp%d, i))," % (str(join_col_type_map[column]), col_right_df_index)
        if len(self.join_col_names) > 1:
            join_col_weld_type = WeldStruct(join_col_field_types)
            join_col_lookup_string = "{" + join_col_lookup_string + "}"
        else:
            join_col_weld_type = join_col_field_types[0]
            join_col_lookup_string = join_col_lookup_string[:-1]

        types_string = "{"
        row_types_list = []
        types_list = []
        right_df_column_names = []
        for i, column in enumerate(right_dataframe):
            col_weld_type: WeldType = numpy_type_to_weld_type(right_dataframe[column].values.dtype)
            if column not in self.join_col_names:
                types_string += str(col_weld_type) + ","
                row_types_list.append(col_weld_type)
                types_list.append(WeldVec(col_weld_type))
                right_df_column_names.append(column)

        types_string = types_string[:-1] + "}"

        values_string = "{"
        for i, column in enumerate(right_dataframe):
            if column not in self.join_col_names:
                values_string += "lookup(_inp%d, i)," % i
        values_string = values_string[:-1] + "}"

        weld_program = \
            """
            result(for(_inp%d,
                dictmerger[JOIN_COL_TYPE, %s, +],
                | bs: dictmerger[JOIN_COL_TYPE, %s, +], i: i64, x |
                    merge(bs, {JOIN_COL_LOOKUP, %s})
            ))
            """ % (0, types_string, types_string, values_string)
        weld_program = weld_program.replace("JOIN_COL_TYPE", str(join_col_weld_type))
        weld_program = weld_program.replace("JOIN_COL_LOOKUP", join_col_lookup_string)

        input_arg_types = {}
        input_arg_list = []
        for i, column in enumerate(right_dataframe):
            input_name = "input%d" % i
            input_arg_types[input_name] = \
                WeldVec(numpy_type_to_weld_type(right_dataframe[column].values.dtype))
            input_arg_list.append(input_name)

        module_name = wexec.compile_weld_program(weld_program,
                                                 input_arg_types,
                                                 input_names=input_arg_list,
                                                 output_names=[],
                                                 base_filename="hash_join_dataframe_indexer")

        hash_join_dataframe_indexer = importlib.import_module(module_name)
        input_args = []
        for column in right_dataframe:
            input_args.append(right_dataframe[column].values)
        input_args = tuple(input_args)

        indexed_right_dataframe = hash_join_dataframe_indexer.caller_func(*input_args)
        self.right_df_row_type = WeldPandas(row_types_list, right_df_column_names)
        self.right_df_type = WeldPandas(types_list, right_df_column_names)
        return [(indexed_right_dataframe, WeldDict(join_col_weld_type, self.right_df_row_type))]

    def push_cascade(self, small_model_output_node: WillumpGraphNode):
        self._small_model_output_name = small_model_output_node.get_output_names()[0]
        self._input_nodes.append(small_model_output_node)
        self._input_names.append(self._small_model_output_name)

    def get_node_weld(self) -> str:
        if self._small_model_output_name is None:
            cascade_statement = "true"
        else:
            cascade_statement = "lookup(%s, i) == 2c" % self._small_model_output_name
        join_col_left_index = self.left_df_type.column_names.index(self.join_col_names[0])
        struct_builder_statement = "{"
        merge_statement = "{"
        merge_zeros_statement = "{"
        result_statement = "{"
        switch = 0
        for i in range(len(self.left_df_type.column_names)):
            result_statement += "%s.$%d," % (self.left_input_name, i)
        for i, column in enumerate(self._right_dataframe):
            if column not in self.join_col_names:
                col_type = str(numpy_type_to_weld_type(self._right_dataframe[column].values.dtype))
                struct_builder_statement += "appender[%s](col_len)," % col_type
                merge_statement += "merge(bs.$%d, right_dataframe_row.$%d)," % (i - switch, i - switch)
                result_statement += "result(pre_output.$%d)," % (i - switch)
                merge_zeros_statement += "merge(bs.$%d, %s(0))," % (i - switch, col_type)
            else:
                switch += 1

        join_col_lookup_string = ""
        for i, column in enumerate(self.left_df_type.column_names):
            if column in self.join_col_names:
                join_col_lookup_string += "lookup(INPUT_NAME.$%d, i)," % i
        if len(self.join_col_names) > 1:
            join_col_lookup_string = "{" + join_col_lookup_string + "}"
        else:
            join_col_lookup_string = "x"

        struct_builder_statement = struct_builder_statement[:-1] + "}"
        merge_statement = merge_statement[:-1] + "}"
        result_statement = result_statement[:-1] + "}"
        merge_zeros_statement = merge_zeros_statement[:-1] + "}"
        weld_program = \
            """
            let col_len = len(INPUT_NAME.$0);
            let pre_output = (for(INPUT_NAME.$JOIN_COL_LEFT_INDEX,
                STRUCT_BUILDER,
                |bs, i: i64, x |
                    if(CASCADE_STATEMENT,
                        let right_dataframe_row_present = optlookup(RIGHT_DATAFRAME_NAME, JOIN_COL_LOOKUP);
                        if(right_dataframe_row_present.$0,
                            let right_dataframe_row = right_dataframe_row_present.$1;
                            MERGE_STATEMENT,
                            MERGE_ZEROS_STATEMENT
                        ),    
                        bs
                    )
            ));
            let OUTPUT_NAME = RESULT_STATEMENT;
            """
        weld_program = weld_program.replace("STRUCT_BUILDER", struct_builder_statement)
        weld_program = weld_program.replace("JOIN_COL_LOOKUP", join_col_lookup_string)
        weld_program = weld_program.replace("MERGE_STATEMENT", merge_statement)
        weld_program = weld_program.replace("RESULT_STATEMENT", result_statement)
        weld_program = weld_program.replace("RIGHT_DATAFRAME_NAME", self._right_dataframe_name)
        weld_program = weld_program.replace("INPUT_NAME", self.left_input_name)
        weld_program = weld_program.replace("OUTPUT_NAME", self._output_name)
        weld_program = weld_program.replace("CASCADE_STATEMENT", cascade_statement)
        weld_program = weld_program.replace("MERGE_ZEROS_STATEMENT", merge_zeros_statement)
        weld_program = weld_program.replace("JOIN_COL_LEFT_INDEX", str(join_col_left_index))
        return weld_program

    def get_output_name(self) -> str:
        return self._output_name

    def get_output_names(self) -> List[str]:
        return [self._output_name]

    def get_output_type(self) -> WeldType:
        return self._output_type

    def get_output_types(self) -> List[WeldType]:
        return [self._output_type]

    def __repr__(self):
        return "Hash-join node for input {0} output {1}\n" \
            .format(self.left_input_name, self._output_name)
