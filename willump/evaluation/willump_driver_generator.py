import os

from weld.types import *
from willump import panic
from willump.willump_utilities import weld_scalar_type_fp

from typing import Mapping, List, Tuple


def generate_cpp_driver(file_version: int, type_map: Mapping[str, WeldType],
                        base_filename: str, aux_data: List[Tuple[int, WeldType]]) -> str:
    """
    Generate a versioned CPP driver for a Weld program. If base_filename is not
    weld_llvm_caller, assume the driver already exists at
    WILLUMP_HOME/cppextensions/base_filename.cpp.  Otherwise, generate a driver using the
    Weld inputs and outputs in type_map.
    """
    willump_home: str = os.environ["WILLUMP_HOME"]
    if base_filename is not "weld_llvm_caller":
        if base_filename is "hash_join_dataframe_indexer":
            buffer = generate_hash_join_dataframe_indexer_driver(type_map)
            buffer = buffer.replace(base_filename, base_filename + str(file_version))
        else:
            with open(os.path.join(willump_home, "cppextensions", base_filename + ".cpp")) as driver:
                buffer = driver.read()
                buffer = buffer.replace(base_filename, base_filename + str(file_version))
    else:
        input_types: List[WeldType] = []
        output_types: List[WeldType] = []
        num_inputs = 0
        num_outputs = 0
        while "__willump_arg{0}".format(num_inputs) in type_map:
            input_types.append(type_map["__willump_arg{0}".format(num_inputs)])
            num_inputs += 1
        while "__willump_retval{0}".format(num_outputs) in type_map:
            output_types.append(type_map["__willump_retval{0}".format(num_outputs)])
            num_outputs += 1
        buffer = ""
        # Header boilerplate.
        with open(os.path.join(willump_home, "cppextensions", "weld_llvm_caller_header.cpp"), "r") as caller_header:
            buffer += caller_header.read()
        # Define the Weld input struct and output struct.
        input_struct = ""
        for i, input_type in enumerate(input_types):
            if isinstance(input_type, WeldStruct):
                inner_struct = ""
                for inner_i, inner_type in enumerate(input_type.field_types):
                    inner_struct += "{0} _{1};\n".format(wtype_to_c_type(inner_type), inner_i)
                buffer += \
                    """
                    struct struct_in_%d {
                      %s
                    };
                    """ % (i, inner_struct)
                input_struct += "struct struct_in_{0} _{1};\n".format(i, i)
            else:
                input_struct += "{0} _{1};\n".format(wtype_to_c_type(input_type), i)
        for (i, (_, input_type)) in enumerate(aux_data):
            input_struct += "{0} _{1};\n".format(wtype_to_c_type(input_type), i + len(input_types))
        output_struct = ""
        for i, output_type in enumerate(output_types):
            if isinstance(output_type, WeldStruct):
                inner_struct = ""
                for inner_i, inner_type in enumerate(output_type.field_types):
                    inner_struct += "{0} _{1};\n".format(wtype_to_c_type(inner_type), inner_i)
                buffer += \
                    """
                    struct struct%d {
                      %s
                    };
                    """ % (i, inner_struct)
                output_struct += "struct struct{0} _{1};\n".format(i, i)
            else:
                output_struct += "{0} _{1};\n".format(wtype_to_c_type(output_type), i)
        buffer += \
            """
            struct struct_in {
              %s
            };
            typedef struct_in input_type;
            struct struct_out {
             %s
            };
            typedef struct_out return_type;
            """ % (input_struct, output_struct)
        # Begin the Weld LLVM caller function.
        buffer += \
            """
            static PyObject *
            caller_func(PyObject *self, PyObject* args) {
            """
        # Generate the input parser
        buffer += generate_input_parser(input_types, aux_data)
        # Create the input arguments and run Weld.
        buffer += \
            """    
            struct WeldInputArgs weld_input_args;
            weld_input_args.input = &weld_input;
            weld_input_args.nworkers = 1;
            weld_input_args.memlimit = 10000000000;
            weld_input_args.run_id = weld_runst_init(weld_input_args.nworkers, weld_input_args.memlimit);
        
            WeldOutputArgs* weld_output_args = run(&weld_input_args);
            return_type* weld_output = (return_type*) weld_output_args->output;
            """
        # Parse Weld outputs and return them.
        if len(output_types) > 1:
            buffer += \
                """
                PyObject* ret_tuple = PyTuple_New(%d);
                """ % (len(output_types))
        for i, output_type in enumerate(output_types):
            if len(output_types) > 1:
                buffer += \
                    """
                    {
                    """
            if isinstance(output_type, WeldStruct):
                buffer += "struct struct%d curr_output = weld_output->_%d;\n" % (i, i)
            else:
                buffer += "%s curr_output = weld_output->_%d;\n" % (wtype_to_c_type(output_type), i)
            if isinstance(output_type, WeldVec) and isinstance(output_type.elemType, WeldStr):
                buffer += \
                    """
                    PyObject* ret = PyList_New(0);
                    for(int i = 0; i < curr_output.size; i++) {
                        i8* str_ptr = curr_output.ptr[i].ptr;
                        i64 str_size = curr_output.ptr[i].size;
                        PyList_Append(ret, PyUnicode_FromStringAndSize((const char *) str_ptr, str_size));
                    }
                    """
            # TODO:  Return a 2-D array instead of a list of 1-D arrays.
            elif isinstance(output_type, WeldVec) and isinstance(output_type.elemType, WeldVec):
                buffer += \
                    """
                    PyObject* ret = PyList_New(0);
                    for(int i = 0; i < curr_output.size; i++) {
                        %s* entry_ptr = curr_output.ptr[i].ptr;
                        i64 entry_size = curr_output.ptr[i].size;
                        PyArrayObject* ret_entry = 
                            (PyArrayObject*) PyArray_SimpleNewFromData(1, &entry_size, %s, entry_ptr);
                        PyArray_ENABLEFLAGS(ret_entry, NPY_ARRAY_OWNDATA);
                        PyList_Append(ret, (PyObject*) ret_entry);
                    }
                    """ % (str(output_type.elemType.elemType), weld_type_to_numpy_macro(output_type.elemType))
            elif isinstance(output_type, WeldVec):
                buffer += \
                    """
                    PyArrayObject* ret = 
                        (PyArrayObject*) PyArray_SimpleNewFromData(1, &curr_output.size, %s, curr_output.ptr);
                    PyArray_ENABLEFLAGS(ret, NPY_ARRAY_OWNDATA);
                    """ % weld_type_to_numpy_macro(output_type)
            elif isinstance(output_type, WeldStruct):
                field_types = output_type.field_types
                buffer += \
                    """
                    PyObject *ret = PyTuple_New(%d);
                    PyObject* ret_entry;
                    """ % len(field_types)
                for inner_i, field_type in enumerate(field_types):
                    if isinstance(field_type, WeldVec):
                        buffer += \
                            """
                            ret_entry = (PyObject*) 
                                PyArray_SimpleNewFromData(1, &curr_output._%d.size, %s, curr_output._%d.ptr);
                            //PyArray_ENABLEFLAGS((PyArrayObject*) ret_entry, NPY_ARRAY_OWNDATA);
                            PyTuple_SetItem(ret, %d, ret_entry);
                            """ % (inner_i, weld_type_to_numpy_macro(field_type), inner_i, inner_i)
                    elif wtype_is_scalar(field_type):
                        if weld_scalar_type_fp(weld_type=field_type):
                            buffer += \
                                """
                                ret_entry =
                                    PyFloat_FromDouble(curr_output._%d);
                                PyTuple_SetItem(ret, %d, ret_entry);
                                """ % (inner_i, inner_i)
                        else:
                            buffer += \
                                """
                                ret_entry =
                                    PyLong_FromLong((long) curr_output._%d);
                                PyTuple_SetItem(ret, %d, ret_entry);
                                """ % (inner_i, inner_i)
                    else:
                        panic("Unrecognized struct field type %s" % str(field_type))

            elif isinstance(output_type, WeldCSR):
                buffer += \
                    """
                    PyArrayObject* rowInd = 
                        (PyArrayObject*) PyArray_SimpleNewFromData(1, 
                        &curr_output.ptr[0].size, %s, curr_output.ptr[0].ptr);
                    PyArray_ENABLEFLAGS(rowInd, NPY_ARRAY_OWNDATA);
                    PyArrayObject* colInd = 
                        (PyArrayObject*) PyArray_SimpleNewFromData(1, 
                        &curr_output.ptr[1].size, %s, curr_output.ptr[1].ptr);
                    PyArray_ENABLEFLAGS(colInd, NPY_ARRAY_OWNDATA);
                    PyArrayObject* retData = 
                        (PyArrayObject*) PyArray_SimpleNewFromData(1, 
                        &curr_output.ptr[2].size, %s, curr_output.ptr[2].ptr);
                    PyArray_ENABLEFLAGS(retData, NPY_ARRAY_OWNDATA);
                    PyObject* ret = PyTuple_New(3);
                    PyTuple_SetItem(ret, 0, (PyObject*) rowInd);
                    PyTuple_SetItem(ret, 1, (PyObject*) colInd);
                    PyTuple_SetItem(ret, 2, (PyObject*) retData);
                    """ % (weld_type_to_numpy_macro(output_type),
                           weld_type_to_numpy_macro(output_type), weld_type_to_numpy_macro(output_type))
            else:
                panic("Unrecognized output type %s" % str(output_type))
            if len(output_types) > 1:
                buffer += \
                    """
                    PyTuple_SetItem(ret_tuple, %d, (PyObject*) ret);
                    }
                    """ % i
        if len(output_types) > 1:
            buffer += \
                """
                    return (PyObject*) ret_tuple;
                }
                """
        else:
            buffer += \
                """
                    return (PyObject*) ret;
                }
                """
        # Footer boilerplate.
        with open(os.path.join(willump_home, "cppextensions", "weld_llvm_caller_footer.cpp"), "r") as footer:
            buffer += footer.read()
        new_function_name = "weld_llvm_caller{0}".format(file_version)
        buffer = buffer.replace("weld_llvm_caller", new_function_name)

    new_file_name = os.path.join(willump_home, "build",
                                 "{0}{1}.cpp".format(base_filename, file_version))
    with open(new_file_name, "w") as outfile:
        outfile.write(buffer)
    return new_file_name


def generate_input_parser(input_types: List[WeldType], aux_data) -> str:
    # Define all input variables.
    buffer = ""
    for i, input_type in enumerate(input_types):
        input_name = "driver_input{0}".format(i)
        if isinstance(input_type, WeldStr):
            buffer += "char* {0} = NULL;\n".format(input_name)
        elif isinstance(input_type, WeldVec):
            if wtype_is_scalar(input_type.elemType):
                input_array_name = "driver_input_array{0}".format(i)
                buffer += \
                    """
                    PyObject* {0} = NULL;
                    PyArrayObject* {1} = NULL;
                    """.format(input_name, input_array_name)
            elif isinstance(input_type.elemType, WeldStr):
                buffer += "PyObject* %s = NULL;\n" % input_name
            else:
                panic("Unsupported input type {0}".format(str(input_type)))
        elif isinstance(input_type, WeldStruct):
            buffer += "PyObject* {0} = NULL;\n".format(input_name)
        elif wtype_is_scalar(input_type):
            buffer += "%s %s;\n" % (str(input_type), input_name)
        else:
            panic("Unsupported input type {0}".format(str(input_type)))
    # Parse all inputs into the input variables.
    format_string = ""
    for input_type in input_types:
        if isinstance(input_type, WeldStr):
            format_string += "s"
        elif isinstance(input_type, WeldVec):
            if wtype_is_scalar(input_type.elemType):
                format_string += "O!"
            else:
                format_string += "O"
        elif isinstance(input_type, WeldStruct):
            format_string += "O"
        elif isinstance(input_type, WeldLong) or isinstance(input_type, WeldInt) or \
                isinstance(input_type, WeldInt16) or isinstance(input_type, WeldChar):
            format_string += "l"
        elif isinstance(input_type, WeldDouble) or isinstance(input_type, WeldFloat):
            format_string += "d"
    acceptor_string = ""
    for i, input_type in enumerate(input_types):
        input_name = "driver_input{0}".format(i)
        if isinstance(input_type, WeldStr) or wtype_is_scalar(input_type) or isinstance(input_type, WeldStruct):
            acceptor_string += ", &{0}".format(input_name)
        elif isinstance(input_type, WeldVec):
            if wtype_is_scalar(input_type.elemType):
                acceptor_string += ", &PyArray_Type, &{0}".format(input_name)
            else:
                acceptor_string += ", &{0}".format(input_name)
    buffer += \
        """
        if (!PyArg_ParseTuple(args, "%s"%s)) {
            return NULL;
        }
        """ % (format_string, acceptor_string)
    # Convert all input Numpy arrays into PyArrayObjects.
    for i, input_type in enumerate(input_types):
        if isinstance(input_type, WeldVec) and wtype_is_scalar(input_type.elemType):
            input_name = "driver_input{0}".format(i)
            input_array_name = "driver_input_array{0}".format(i)
            buffer += \
                """
                if ((%s = (PyArrayObject *) PyArray_FROM_OTF(%s , %s, NPY_ARRAY_IN_ARRAY)) == NULL) {
                    return NULL;
                }
                """ % (input_array_name, input_name, weld_type_to_numpy_macro(input_type))
    # Find the length of all vector inputs.
    for i, input_type in enumerate(input_types):
        input_len_name = "input_len%d" % i
        if isinstance(input_type, WeldStr):
            input_name = "driver_input{0}".format(i)
            buffer += "int %s = strlen(%s);\n" % (input_len_name, input_name)
        elif isinstance(input_type, WeldVec) and wtype_is_scalar(input_type.elemType):
            input_array_name = "driver_input_array{0}".format(i)
            buffer += "int %s = PyArray_DIMS(%s)[0];\n" % (input_len_name, input_array_name)
    # Define all the entries of the weld input struct.
    buffer += "input_type weld_input;\n"
    for i, input_type in enumerate(input_types):
        input_len_name = "input_len%d" % i
        input_name = "driver_input{0}".format(i)
        input_array_name = "driver_input_array{0}".format(i)
        weld_input_name = "weld_input%d" % i
        if isinstance(input_type, WeldStr):
            buffer += \
                """
                vec<i8> {0};
                {0}.size = {1};
                {0}.ptr = (i8*) {2};
                """.format(weld_input_name, input_len_name, input_name)
        elif isinstance(input_type, WeldVec):
            if wtype_is_scalar(input_type.elemType):
                buffer += \
                    """
                    vec<{3}> {0};
                    {0}.size = {1};
                    {0}.ptr = ({3}*) PyArray_DATA({2});
                    """.format(weld_input_name, input_len_name,
                               input_array_name, wtype_to_c_type(input_type.elemType))
            else:
                buffer += \
                    """
                    vec<vec<i8>> %s;
                    %s.size = PyList_Size(%s);
                    %s.ptr = (vec<i8>*) malloc(sizeof(vec<i8>) * %s.size);
                    for(int i = 0; i < %s.size; i++) {
                        PyObject* string_entry = PyList_GetItem(%s, i);
                        %s.ptr[i].size = PyUnicode_GET_LENGTH(string_entry);
                        %s.ptr[i].ptr = (i8*) PyUnicode_DATA(string_entry);
                    }
                    """ % (weld_input_name, weld_input_name, input_name, weld_input_name, weld_input_name,
                           weld_input_name, input_name, weld_input_name, weld_input_name)
        elif isinstance(input_type, WeldStruct):
            buffer += \
                """
                struct struct_in_%d %s;
                """ % (i, weld_input_name)
            for inner_i, field_type in enumerate(input_type.field_types):
                buffer += \
                    """
                    PyObject* weld_entry{0} = PyTuple_GetItem({1}, {0});
                    """.format(inner_i, input_name)
                if isinstance(field_type, WeldVec):
                    buffer += \
                        """
                        PyArrayObject* weld_numpy_entry%d;
                        if ((weld_numpy_entry%d = (PyArrayObject *) PyArray_FROM_OTF(weld_entry%d , %s, 
                            NPY_ARRAY_IN_ARRAY)) == NULL) {
                            return NULL;
                        }
                        """ % (inner_i, inner_i, inner_i, weld_type_to_numpy_macro(field_type))
                    buffer += \
                        """
                        {0}._{1}.size = PyArray_DIMS(weld_numpy_entry{1})[0];
                        {0}._{1}.ptr = ({2}*) PyArray_DATA(weld_numpy_entry{1});
                        """.format(weld_input_name,
                                   inner_i, wtype_to_c_type(field_type.elemType))
                elif wtype_is_scalar(field_type):
                    if weld_scalar_type_fp(weld_type=field_type):
                        buffer += \
                            """
                            %s._%d = PyFloat_AS_DOUBLE(weld_entry%d);
                            """ % (weld_input_name, inner_i, inner_i)
                    else:
                        buffer += \
                            """
                            %s._%d = PyLong_AsLong(weld_entry%d);
                            """ % (weld_input_name, inner_i, inner_i)
                else:
                    panic("Unrecognized struct field type %s" % str(field_type))
        elif wtype_is_scalar(input_type):
            buffer += "%s %s = %s;\n" % (wtype_to_c_type(input_type), weld_input_name, input_name)
        buffer += "weld_input._%d = %s;\n" % (i, weld_input_name)
    # Also make inputs out of the aux_data pointers so Weld knows where the data structures are.
    for (aux_i, (input_pointer, input_type)) in enumerate(aux_data):
        i = aux_i + len(input_types)
        weld_input_name = "weld_input%d" % i
        if isinstance(input_type, WeldStr):
            pass
        elif isinstance(input_type, WeldVec):
            buffer += \
                """
                {0}* {1} = ({0}*) {3};
                weld_input._{2}.size = {1}->size;
                weld_input._{2}.ptr = {1}->ptr;
                """.format(wtype_to_c_type(input_type), weld_input_name, i, hex(input_pointer))
        elif isinstance(input_type, WeldDict):
            buffer += "weld_input._%d = (void*) %s;\n" % (i, hex(input_pointer))
    return buffer


def wtype_is_scalar(wtype: WeldType) -> bool:
    if isinstance(wtype, WeldLong) or isinstance(wtype, WeldInt) or isinstance(wtype, WeldInt16) or \
            isinstance(wtype, WeldChar) or isinstance(wtype, WeldDouble) or isinstance(wtype, WeldFloat):
        return True
    else:
        return False


def wtype_to_c_type(wtype: WeldType) -> str:
    """
    Return the C type used to represent a Weld type in the driver.
    """
    if isinstance(wtype, WeldVec) or isinstance(wtype, WeldStr):
        return "vec<{0}>".format(wtype_to_c_type(wtype.elemType))
    elif isinstance(wtype, WeldDict):
        return "void*"
    elif isinstance(wtype, WeldCSR):
        return "vec<vec<{0}>>".format(wtype_to_c_type(wtype.elemType))
    else:
        return str(wtype)


def weld_type_to_numpy_macro(wtype: WeldType) -> str:
    """
    Convert a Weld type into a string to plug into the C++ driver.  Currently assumes all types
    are vectors and returns the type of the elements.

    TODO:  More types.
    """
    if isinstance(wtype, WeldVec) or isinstance(wtype, WeldCSR):
        if isinstance(wtype.elemType, WeldDouble):
            return "NPY_FLOAT64"
        elif isinstance(wtype.elemType, WeldFloat):
            return "NPY_FLOAT32"
        elif isinstance(wtype.elemType, WeldChar):
            return "NPY_INT8"
        elif isinstance(wtype.elemType, WeldInt16):
            return "NPY_INT16"
        elif isinstance(wtype.elemType, WeldInt):
            return "NPY_INT32"
        elif isinstance(wtype.elemType, WeldLong):
            return "NPY_INT64"
        else:
            panic("Unrecognized IO type {0}".format(wtype.__str__()))
            return ""
    elif isinstance(wtype, WeldStr):
        return "NPY_INT8"
    else:
        panic("Numpy array type that is not vector {0}".format(wtype.__str__()))
        return ""


def generate_hash_join_dataframe_indexer_driver(type_map: Mapping[str, WeldType]) -> str:
    willump_home: str = os.environ["WILLUMP_HOME"]
    with open(os.path.join(willump_home, "cppextensions", "hash_join_dataframe_indexer.cpp")) as driver:
        buffer = driver.read()
    input_types: List[WeldType] = []
    num_inputs = 0
    while "__willump_arg{0}".format(num_inputs) in type_map:
        input_types.append(type_map["__willump_arg{0}".format(num_inputs)])
        num_inputs += 1
    input_struct = ""
    for i, input_type in enumerate(input_types):
        input_struct += "{0} _{1};\n".format(wtype_to_c_type(input_type), i)
    buffer = buffer.replace("INPUT_STRUCT_CONTENTS", input_struct)
    buffer = buffer.replace("INPUT_PARSING_CONTENTS", generate_input_parser(input_types, []))
    return buffer
