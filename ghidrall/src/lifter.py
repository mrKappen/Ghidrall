from llvmlite import ir
from llvmlite import binding as llvm
import xml.etree.ElementTree as et

instrumentation_list = ["sym.path_start", "sym.path_goal", "sym.path_nongoal", "sym.imp.rand"]
int32 = ir.IntType(32)
int64 = ir.IntType(64)
int1 = ir.IntType(1)
int8 = ir.IntType(8)
void_type = ir.VoidType()


def lift_binary(decompile_info, filename, lifting_options):
    """Initiates a Lifter object with a decompiled binary and lifts"""
    ir.global_context.identified_types = {}
    lifter = Lifter(decompile_info, filename, lifting_options)
    return lifter.module


class Lifter:
    """Decompiler class for interfacing with Radare2 and pulling decompilation information in XML"""

    def __init__(self, decompile_info, filename, lifting_options):
        self.filename = filename
        self.module = ir.Module(name=filename)
        self.options = lifting_options
        self.module.data_layout = "e-m:e-i64:64-f80:128-n8:16:32:64-S128"
        self.module.triple = "x86_64-pc-linux-gnu"
        self.decompile_info = decompile_info
        self.global_vars = self.fish_globals()
        self.instrumentation = {}
        self.function_names = list(self.decompile_info.keys())
        self.functions_ir, self.functions_xml, self.functions_args, self.ret_types = self.create_function_signatures()
        self.populate_functions()

    def fish_globals(self):
        """Find globals as identified by r2 given ".obj" identifier"""
        global_vars = {}
        for decompile_info in list(self.decompile_info.values()):
            pdg = decompile_info.splitlines()
            i = 0
            for line in pdg:
                if "<symbol>_obj" in line or "<symbol>obj" in line:
                    symbol = line.split('<symbol>')[1].split('</symbol>')[0]
                    if symbol not in list(global_vars.keys()):
                        size_line = pdg[i + 2]
                        size = ir.IntType(int(size_line.split('<size>')[1].split('</size>')[0]) * 8)
                        glob = ir.GlobalVariable(self.module, size, symbol)
                        glob.initializer = size(0)
                        global_vars[symbol] = glob
                i += 1
        return global_vars

    
    def create_function_signatures(self):
        """"Create the function signatures before building instructions and basic blocks so function calls resolve"""
        all_ir, all_xml, all_args, return_types = {}, {}, {}, {}
        for function in self.function_names:
            function_pdg = self.decompile_info[function]
            function_xml = et.fromstring(function_pdg)
            xml_args = function_xml.find('args').findall('var')
            xml_return_type = function_xml.find('return')
            return_types[function] = "void"
            if xml_return_type.find('type').text == "undefined8":
                func_return = ir.IntType(64)
                return_types[function] = "undefined8"
            elif xml_return_type.find('type').text == "void":
                func_return = void_type
            else:
                func_return = ir.IntType(8 * int(xml_return_type.find('size').text))
                return_types[function] = 8 * int(xml_return_type.find('size').text)
            args = {}
            for arg in xml_args:
                name = arg.find('name').text
                args[name] = (ir.IntType(8 * int(arg.find('size').text)))
            func_type = ir.FunctionType(func_return, list(args.values()))
            ir_func = ir.Function(self.module, func_type, function)
            for i in range(len(ir_func.args)):
                ir_func.args[i].name = list(args.keys())[i]
            all_xml[function] = function_xml
            all_ir[function] = ir_func
            all_args[function] = args
        return all_ir, all_xml, all_args, return_types

    def populate_functions(self):
        """"Given a function signature, build basic blocks and instructions"""
        for function in self.function_names:
            func = Function(function,
                            self.functions_ir[function],
                            self.functions_xml[function],
                            self.functions_args[function],
                            self.ret_types[function], self)
            func.lift_function()


class Function:
    """"Holds the relevant data for a function"""
    def __init__(self, function_name, ir_func, xml, args, return_type, lifter):
        """"Initialize function variables, recover locals, and build the CFG"""
        self.name = function_name
        self.ir_func = ir_func
        self.local_sizes = {}
        self.xml = xml
        self.args = args
        self.return_type = return_type
        self.lifter = lifter
        self.options = lifter.options
        self.temps = {}
        self.entry_block, self.locals, self.entry_builder = self.recover_locals()
        self.ir_blocks, self.xml_blocks = self.build_cfg()

    def recover_locals(self):
        """"Recover stack variables based on decided structure from lifting options. Also recovers register types"""
        local_vars = {}
        entry_block = self.ir_func.append_basic_block("entry")
        entry_builder = ir.IRBuilder(entry_block)
        if self.options["stack"] == "single_struct":
            local_context = ir.global_context
            local_struct = local_context.get_identified_type("struct.locals." + self.lifter.filename + "." + self.name)
            local_sizes = {}
            for local in self.xml.find("locals").findall('var'):
                name = local.find("name").text
                if "bVar" in name:
                    size = 1
                else:
                    size = int(local.find("size").text) * 8
                local_sizes[name] = ir.IntType(size)
            local_struct.set_body(*list(local_sizes.values()))
            struct = entry_builder.alloca(local_struct)
            for index, name in enumerate(list(local_sizes.keys())):
                local_vars[name] = entry_builder.gep(struct, [int32(0), int32(index)], inbounds=True, name=name)
        elif self.options["stack"] == "byte_addressable":
            local_context = ir.global_context
            local_struct = local_context.get_identified_type("struct.locals." + self.lifter.filename + "." + self.name)
            local_sizes = []
            total_size = 0
            for local in self.xml.find("locals").findall('var'):
                name = local.find("name").text
                if "bVar" in name:
                    size = 1
                    bit_size = 1
                else:
                    size = int(local.find("size").text)
                    bit_size = size * 8
                local_sizes.append((name, total_size, ir.IntType(bit_size)))  # total_size is index here
                total_size += size  # size in bytes
            local_struct.set_body(*[int8 for _ in range(total_size)])
            struct = entry_builder.alloca(local_struct)
            for name, index, bits in local_sizes:
                temp = entry_builder.gep(struct, [int32(0), int32(index)], inbounds=True)
                local_vars[name] = entry_builder.bitcast(temp, ir.PointerType(bits), name=name)
        elif self.options['stack'] == "no_option":
            for local in self.xml.find("locals").findall('var'):
                name = local.find("name").text
                if "bVar" in name:
                    size = 1
                else:
                    size = int(local.find("size").text) * 8
                local_vars[name] = entry_builder.alloca(ir.IntType(size), name=name)
        # Recover register types
        register_vars = {}
        pdg = et.tostring(self.xml, encoding="unicode").splitlines()
        i = 0
        for line in pdg:
            if "register0x" in line:
                symbol = line.split('<symbol>')[1].split('</symbol>')[0]
                size_line = pdg[i + 2]
                size = int(size_line.split('<size>')[1].split('</size>')[0])
                if size != 1:
                    size *= 8
                if symbol not in list(register_vars.keys()):
                    register_vars[symbol] = size
                elif size > register_vars[symbol]:
                    register_vars[symbol] = size
            i += 1
        for symbol, size in register_vars.items():
            local_vars[symbol] = entry_builder.alloca(ir.IntType(size), name=symbol)
        return entry_block, local_vars, entry_builder

    def build_cfg(self):
        """Build the CFG of the function"""
        ir_blocks, xml_blocks = {}, {}
        for xml_block in self.xml.find("block_graph").findall("block"):
            label = self.format_label(xml_block.find("label").find("address").text)
            block = self.ir_func.append_basic_block(label)
            ir_blocks[label] = block
            xml_blocks[label] = xml_block
        self.entry_builder.branch(list(ir_blocks.values())[0])
        return ir_blocks, xml_blocks
    def fixConflictingIntType(self,lhs,rhs):
        widthLhs = lhs.type.width
        widthRhs = rhs.type.width
        if widthLhs > widthRhs:
            rhs.type = ir.IntType(widthLhs)
        else:
            lhs.type = ir.IntType(widthRhs)
        return lhs, rhs
        pass
    def lift_function(self):
        """Populate the CFG with instructions"""
        for label in list(self.ir_blocks.keys()):
            ir_block = self.ir_blocks[label]
            xml_block = self.xml_blocks[label]
            builder = ir.IRBuilder(ir_block)
            branched = False
            for instruction in xml_block.find("ops").findall("op"):
                opname = instruction.find("opname").text
                if opname == "COPY":
                    input_target = instruction.find("inputs").find("input")
                    target = instruction.find("output")
                    result = self.fetch_input(builder, input_target, self.temps, self.ir_func, self.locals,
                                              self.lifter.global_vars)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "LOAD":
                    pass
                elif opname == "STORE":
                    pass
                elif opname == "BRANCH":
                    target = self.format_label(instruction.find("inputs").find("input").find("symbol").text)
                    out_target = self.format_label(xml_block.find("out_branches").findall("branch_target")[0].text)
                    if target != out_target:
                        target = out_target
                    builder.branch(self.ir_blocks[target])
                    branched = True
                elif opname == "CBRANCH":
                    inputs = instruction.find("inputs").findall("input")
                    true_branch = self.format_label(inputs[0].find("symbol").text)
                    conditional = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                                   self.lifter.global_vars)
                    false_branch = None
                    branches = xml_block.find("out_branches").findall("branch_target")
                    true_branch_off_by_1 = self.format_label(hex(int(true_branch, 16) + 1))
                    if true_branch not in branches or true_branch_off_by_1 not in branches:
                        true_branch = branches[0].text
                        false_branch = branches[1].text
                    else:
                        for branch in branches:
                            if branch.text == true_branch:
                                pass
                            elif branch.text == true_branch_off_by_1:
                                true_branch = branch.text
                            else:
                                false_branch = branch.text
                    if false_branch is None:
                        raise Exception("No block match in conditional branch")
                    builder.cbranch(conditional, self.ir_blocks[false_branch], self.ir_blocks[true_branch])
                    branched = True
                elif opname == "BRANCHIND":
                    inputs = instruction.find("inputs").findall("input")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    builder.goto_block(lhs)
                elif opname == "CALL":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    call_target = inputs[0].find("symbol").text
                    args = []
                    result = None
                    for arg in inputs[1:]:
                        a = self.fetch_input(builder, arg, self.temps, self.ir_func, self.locals,
                                                     self.lifter.global_vars)
                        if a is not None:
                            args.append(a)
                    if call_target in instrumentation_list:
                        func_call = self.instrument(call_target, self.lifter)
                        if func_call is not None:
                            result = builder.call(func_call, [])
                    else:
                        if len(self.lifter.functions_ir[call_target].args) == 0:
                            result = builder.call(self.lifter.functions_ir[call_target], args=[])
                        else:
                            result = builder.call(self.lifter.functions_ir[call_target], args=args)
                    if result is not None and target is not None:
                        self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                self.lifter.global_vars)
                elif opname == "CALLIND":
                    # create an empty function declaration
                    inputs = instruction.find("inputs").findall("input")
                    call_target = inputs[0].find("symbol").text
                    func_type = ir.FunctionType(ir.VoidType(), [])
                    if call_target not in builder.module.globals:
                        ir_func = ir.Function(self.lifter.module, func_type, call_target)
                    else:
                        ir_func = builder.module.globals[call_target]
                    builder.call(ir_func, [])
                # elif opname == "CALLOTHER":
                #     raise Exception("Not implemented: " + opname)
                elif opname == "RETURN":
                    if self.return_type == "void":
                        builder.ret_void()
                    else:
                        inputs = instruction.find("inputs").findall("input")
                        lhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                               self.lifter.global_vars)
                        builder.ret(lhs)
                    branched = True
                elif opname == "INT_EQUAL":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.icmp_unsigned('==', lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_NOTEQUAL":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.icmp_unsigned('!=', lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_SLESS":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.icmp_signed('<', lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_SLESSEQUAL":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.icmp_signed('<=', lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_LESS":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.icmp_unsigned('<', lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_LESSEQUAL":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.icmp_unsigned('<=', lhs, rhs)
                elif opname == "INT_ZEXT":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    target_type = ir.IntType(8 * int(target.find("size").text))
                    result = builder.zext(lhs, target_type)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_SEXT":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    target_type = ir.IntType(8 * int(target.find("size").text))
                    result = builder.sext(lhs, target_type)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_ADD":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.add(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_SUB":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.sub(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                # elif opname == "INT_CARRY":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "INT_SCARRY":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "INT_SBORROW":
                #     raise Exception("Not implemented: " + opname)
                elif opname == "INT_2COMP":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    input0 = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    result = builder.neg(input0)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                # elif opname == "INT_NEGATE":
                #     raise Exception("Not implemented: " + opname)
                elif opname == "INT_XOR":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    input0 = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    input1 = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    result = builder.xor(input0,input1)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_AND":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.and_(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_OR":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    input0 = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    input1 = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.or_(input0,input1)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars) 
                elif opname == "INT_LEFT":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.shl(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_RIGHT":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.lshr(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_SRIGHT":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.ashr(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_MULT":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.mul(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_DIV":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.udiv(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_SDIV":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.sdiv(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_REM":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.urem(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "INT_SREM":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    if lhs.type != rhs.type and isinstance(lhs.type,ir.IntType) and isinstance(rhs.type,ir.IntType):
                        lhs,rhs = self.fixConflictingIntType(lhs,rhs)
                    result = builder.srem(lhs, rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "BOOL_NEGATE":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    result = builder.not_(lhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "BOOL_XOR":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    result = builder.xor(lhs,rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "BOOL_AND":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    result = builder.and_(lhs,rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "BOOL_OR":
                    inputs = instruction.find("inputs").findall("input")
                    target = instruction.find("output")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                           self.lifter.global_vars)
                    result = builder.or_(lhs,rhs)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                # elif opname == "FLOAT_EQUAL":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_NOTEQUAL":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_LESS":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_LESSEQUAL":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_NAN":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_ADD":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_DIV":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_MULT":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_SUB":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_NEG":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_ABS":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_SQRT":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_INT2FLOAT":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_FLOAT2FLOAT":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_TRUNC":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_CEIL":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_FLOOR":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "FLOAT_ROUND":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "MULTIEQUAL":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "INDIRECT":
                #     raise Exception("Not implemented: " + opname)
                elif opname == "PIECE":
                    target = instruction.find("output")
                    output_size = 8*int(instruction.find("output").find("size").text)
                    inputs = instruction.find("inputs").findall("input")
                    least_sig_size = 8*int(instruction.find("inputs").findall("input")[1].find("size").text)
                    input_most_sig = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    input_least_sig = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    input_least_sig = builder.zext(input_least_sig,ir.IntType(output_size))
                    input_most_sig = builder.zext(input_most_sig,ir.IntType(output_size))
                    input_most_sig = builder.shl(input_most_sig,ir.Constant(ir.IntType(output_size),least_sig_size))
                    result = builder.or_(input_least_sig,input_most_sig)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "SUBPIECE":
                    output = instruction.find("output")
                    inputs = instruction.find("inputs").findall("input")
                    argument = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    offset = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                              self.lifter.global_vars)
                    if inputs[1].find("symbol").text == "0":
                        result = builder.trunc(argument, ir.IntType(int(inputs[1].find("size").text) * 8))
                    else:
                        raise Exception("Unexpected subpiece operation")
                    self.fetch_store_output(builder, output, result, self.temps, self.locals, self.lifter.global_vars)
                elif opname == "CAST":
                    input_target = instruction.find("inputs").find("input")
                    target = instruction.find("output")
                    result = self.fetch_input(builder, input_target, self.temps, self.ir_func, self.locals,
                                              self.lifter.global_vars)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "PTRADD":
                    target = instruction.find("output")
                    inputs = instruction.find("inputs").findall("input")
                    input0 = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    input1 = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    input2 = self.fetch_input(builder, inputs[2], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    input1_times_input2 = builder.mul(input1,input2)
                    result = builder.add(input0,input1_times_input2)
                    output = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                elif opname == "PTRSUB":
                    target = instruction.find("output")
                    inputs = instruction.find("inputs").findall("input")
                    lhs = self.fetch_input(builder, inputs[0], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    rhs = self.fetch_input(builder, inputs[1], self.temps, self.ir_func, self.locals,
                                                self.lifter.global_vars)
                    result = builder.add(lhs,rhs)
                    output  = self.fetch_store_output(builder, target, result, self.temps, self.locals,
                                                     self.lifter.global_vars)
                # elif opname == "SEGMENTOP":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "CPOOLREF":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "NEW":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "INSERT":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "EXTRACT":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "POPCOUNT":
                #     raise Exception("Not implemented: " + opname)
                # elif opname == "MAX":
                #     raise Exception("Not implemented: " + opname)
                else:
                    raise Exception("Not expected: " + opname)

            if not branched:
                builder.branch(self.ir_blocks[xml_block.find("out_branches").find("branch_target").text])

    @staticmethod
    def format_label(label):
        """Helper function for formatting hexidecimal addresses"""
        if label[:2] == '0x':
            label = label[2:]
        new_label = '0x' + label.zfill(8)
        return new_label

    @staticmethod
    def instrument(call_target, lifter):
        """Instrumentation features for test cases based on Pharos queries to convert to Seahorn"""
        module = lifter.module
        if call_target in lifter.instrumentation:
            return lifter.instrumentation[call_target]
        if call_target == "sym.path_start":
            ir_func = None
        elif call_target == "sym.path_goal":
            args = []
            func_type = ir.FunctionType(void_type, args)
            ir_func = ir.Function(module, func_type, "verifier.error")
            lifter.instrumentation[call_target] = ir_func
        elif call_target == "sym.path_nongoal":
            args = []
            func_type = ir.FunctionType(void_type, args)
            ir_func = ir.Function(module, func_type, "verifier.error")
            lifter.instrumentation[call_target] = ir_func
        elif call_target == "sym.imp.rand":
            args = []
            func_type = ir.FunctionType(int32, args)
            ir_func = ir.Function(module, func_type, "nd")
            lifter.instrumentation[call_target] = ir_func
        else:
            raise Exception("Not excepted instrumentation function")
        return ir_func

    def fetch_input(self, builder, arg, temps, ir_func, local_vars, global_vars):
        """Fetch the input of a single instruction"""
        symbol = arg.find("symbol").text
        if "var" in symbol:
            symbol = symbol.split('.')[0]
        if "bVar" in symbol:
            size = 1
        else:
            size = int(arg.find("size").text)
            if size != 1:
                size *= 8
        if "argv" in symbol:
            raise Exception("argc and argv weird behaviour")
        if "argc" in symbol:
            raise Exception("argc and argv weird behaviour")
        if "arg" in symbol:
            for arg in ir_func.args:
                if arg.name == symbol:
                    return arg
        if symbol in global_vars:
            return builder.load(global_vars[symbol])
        if symbol in local_vars:
            if symbol in self.local_sizes:
                return builder.load(local_vars[symbol])
            offset = 0
            try:
                offset = 8 * int(arg.find("symbol").get("offset"))
                offset_size = 8 * int(arg.find("symbol").get("size"))
                success = True
            except:
                offset_size = size
                success = False
            if success:
                output = builder.gep(local_vars[symbol], [ir.Constant(ir.IntType(offset_size), offset)])
            else:
                output = local_vars[symbol]
            final = builder.load(output)
            if final.type != ir.IntType(offset_size):
                final = builder.trunc(final, ir.IntType(offset_size))
            return final
        if symbol in temps:
            if temps[symbol].type == void_type:
                return ir.Constant(ir.IntType(1), 0)
            elif temps[symbol].type != ir.IntType(size) and temps[symbol].type != int1:
                result = builder.trunc(temps[symbol], ir.IntType(size))
                return result
            else:
                return temps[symbol]
        if "false" in symbol:
            return ir.Constant(ir.IntType(1), 0)
        if "true" in symbol:
            return ir.Constant(ir.IntType(1), 1)
        else:
            try:
                if "U" in symbol:
                    symbol = symbol.split('U')[0]
                if "0x" in symbol:
                    index = symbol.index("0x")
                    val = int(symbol[index:], 16)
                else:
                    val = int(symbol)
                return ir.Constant(ir.IntType(size), val)
            except:
                return ir.Constant(ir.IntType(size), 0)

    def fetch_store_output(self, builder, arg, result, temps, local_vars, global_vars):
        """"Fetch the output of a given instruction"""
        symbol = arg.find("symbol").text
        if "var" in symbol:
            symbol = symbol.split('.')[0]
        size = 8 * int(arg.find("size").text)
        offset = 0
        try:
            offset = 8 * int(arg.find("symbol").get("offset"))
            offset_size = 8 * int(arg.find("symbol").get("size"))
            success = True
        except:
            offset_size = size
            success = False
        if symbol in local_vars:
            if success:
                output = builder.gep(local_vars[symbol], [ir.Constant(ir.IntType(offset_size), offset)])
            else:
                output = local_vars[symbol]
            if result.type != void_type:
                if output.type != result.type.as_pointer():
                    output = builder.bitcast(output, result.type.as_pointer())
                return builder.store(result, output)
            else:
                return None
        elif symbol in global_vars:
            return builder.store(result, global_vars[symbol])
        elif "register" in symbol:
            temps[symbol] = result
        elif "unique" in symbol:
            temps[symbol] = result
        elif "arg" in symbol:
            #TODO:implement
            pass
        else:
            pass
            # raise Exception("Unexpected target varnode: ",symbol)