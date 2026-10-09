"""Generate whitelisted Rust DLL calls from the checked SDK ABI and buffers."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import operator
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = {"esi": "esi", "psu": "psu_a", "amx": "amx_a",
            "amx_hd": "amx_hd", "ampr": "ampr_a", "dmmr": "dmmr"}
TYPES = {"BYTE": "u8", "WORD": "u16", "DWORD": "u32", "unsigned": "u32",
         "BOOL": "i32", "bool": "u8", "double": "f64", "float": "f32",
         "int": "i32", "char": "u8"}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.LShift: operator.lshift, ast.RShift: operator.rshift,
       ast.BitOr: operator.or_, ast.BitAnd: operator.and_, ast.FloorDiv: operator.floordiv,
       ast.Div: operator.floordiv}


def constant(node, names):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name):
        return names[node.id]
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return names[node.attr]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Invert)):
        return {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Invert: operator.invert}[type(node.op)](constant(node.operand, names))
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        return OPS[type(node.op)](constant(node.left, names), constant(node.right, names))
    raise ValueError(f"Unsupported constant expression {ast.dump(node)}")


def header_constants(text):
    values = {}
    definitions = re.findall(r"^\s*#define\s+(\w+)\s+([^\n]+)", text, re.M)
    for _ in range(len(definitions)):
        changed = False
        for name, expr in definitions:
            if name in values:
                continue
            expr = re.sub(r"(?<=\d)[uUlL]+\b", "", expr)
            try:
                value = constant(ast.parse(expr.strip(), mode="eval").body, values)
            except (ValueError, KeyError, SyntaxError):
                continue
            values[name] = value
            changed = True
        if not changed:
            break
    return values


def buffer_lengths(tree):
    names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                names[node.targets[0].id] = constant(node.value, names)
            except (ValueError, KeyError, TypeError):
                pass
    result = {}
    for method in (node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)):
        buffers = {}
        for node in ast.walk(method):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if isinstance(call.func, ast.Attribute) and call.func.attr == "create_string_buffer":
                try:
                    size = int(constant(call.args[-1], names))
                except (ValueError, KeyError, IndexError):
                    continue
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        buffers[target.id] = size
        for node in ast.walk(method):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or not node.func.attr.startswith("COM_"):
                continue
            for index, argument in enumerate(node.args):
                if isinstance(argument, ast.Name) and argument.id in buffers:
                    key = node.func.attr, index
                    if key in result and result[key] != buffers[argument.id]:
                        raise ValueError(f"Conflicting buffer sizes for {key}")
                    result[key] = buffers[argument.id]
    return result


def declarations(family, folder):
    base = ROOT / folder / "vendor" / "runtime" / family / f"{family}_base.py"
    header = next((base.parent / "vendor").glob("*.h"))
    tree = ast.parse(base.read_text())
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr.startswith("COM_")}
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", header.read_text(), flags=re.S)
    text = re.sub(r"\b(?:FAR|_export)\b", "", text)
    constants = header_constants(text)
    buffers = buffer_lengths(tree)
    functions = {}
    for match in re.finditer(r"^[ \t#]*(WORD|int|const\s+char\s*\*)\s*(COM_\w+)\s*\(([^;]*)\);", text, re.M):
        result, name, arguments = match.groups()
        if name not in used:
            continue
        params = []
        for index, argument in enumerate(arguments.split(",") if arguments.strip() else []):
            argument = re.sub(r"\bconst\b", "", argument)
            parsed = re.fullmatch(r"\s*(unsigned|WORD|DWORD|BYTE|BOOL|bool|double|float|int|char)\s*([&*]?)\s*(\w+)\s*(?:\[([^\]]+)\])?\s*", argument)
            if not parsed:
                raise ValueError(f"Unsupported SDK argument {header}: {argument}")
            scalar, reference, param, array = parsed.groups()
            pointer = bool(reference or array)
            size = int(constant(ast.parse(array.strip(), mode="eval").body, constants)) if array else 1
            if scalar == "char" and pointer and not array:
                size = buffers.get((name, index), 0)
                if not size:
                    raise ValueError(f"Missing checked string buffer size: {name}:{index}")
            if not 1 <= size <= 65536:
                raise ValueError(f"Unsafe SDK buffer size: {name}:{index}: {size}")
            params.append({"type": scalar, "pointer": pointer, "length": size, "name": param})
        signature = {"args": params, "result": re.sub(r"\s+", "", result)}
        if name in functions and functions[name] != signature:
            raise ValueError(f"Conflicting SDK declarations: {name}")
        functions[name] = signature
    if used - functions.keys():
        raise ValueError(f"Undocumented exports: {used - functions.keys()}")
    if family == "dmmr":
        functions["COM_DMMR_8_OpenDebugFile"] = {"args": [{"type": "char", "pointer": True, "length": 4096, "input": True, "name": "FileName"}], "result": "int", "verified_dll_sha256": "e3bb4674f88e5a894c36cdb5e544f0b5a35691c2a0dbdc56719c9928f05bcea3"}
        functions["COM_DMMR_8_CloseDebugFile"] = {"args": [], "result": "int", "verified_dll_sha256": "e3bb4674f88e5a894c36cdb5e544f0b5a35691c2a0dbdc56719c9928f05bcea3"}
    return functions, {"header": str(header.relative_to(ROOT)), "header_sha256": hashlib.sha256(header.read_bytes()).hexdigest(), "base_sha256": hashlib.sha256(base.read_bytes()).hexdigest()}


def rust_source(functions):
    lines = ["// Generated by tools/generate_native_abi.py; do not edit.",
             "use crate::ffi::*;", "use crate::error::{Error, Result};",
             "use serde_json::Value;", "use libloading::Library;", "",
             "pub fn dispatch(lib: &Library, symbol: &str, args: &[Value]) -> Result<NativeReply> {",
             "    match symbol {"]
    for symbol, spec in sorted(functions.items()):
        lines += [f'        "{symbol}" => {{', f'            arity(args, {len(spec["args"])}, symbol)?;']
        c_types, expressions, outputs = [], [], []
        for i, param in enumerate(spec["args"]):
            typ, pointer, length = param["type"], param["pointer"], param["length"]
            rust = TYPES[typ]
            boolean = str(typ in {"bool", "BOOL"}).lower()
            c_types.append(f"*mut {rust}" if pointer else rust)
            if pointer:
                if typ == "char":
                    if param.get("input"):
                        lines.append(f"            let mut a{i} = text_input(&args[{i}], {length})?;")
                    else:
                        lines.append(f"            let mut a{i} = text_buffer(&args[{i}], {length})?;")
                        outputs.append(f"text_output(&a{i})")
                else:
                    lines.append(f"            let mut a{i} = pointer::<{rust}>(&args[{i}], {length}, {boolean})?;")
                    outputs.append(f"pointer_output(&a{i}, &args[{i}], {boolean})")
                expressions.append(f"a{i}.as_mut_ptr()")
            else:
                lines.append(f"            let a{i} = scalar::<{rust}>(&args[{i}], {boolean})?;")
                expressions.append(f"a{i}")
        result = "*const std::ffi::c_char" if spec["result"] == "constchar*" else TYPES[spec["result"]]
        lines += [f'            let function = unsafe {{ lib.get::<unsafe extern "system" fn({", ".join(c_types)}) -> {result}>(b"{symbol}\\0") }}.map_err(|e| Error::new("AttributeError", format!("Missing vendor export {{symbol}}: {{e}}")))?;',
                  f'            let status = unsafe {{ function({", ".join(expressions)}) }};']
        if spec["result"] == "constchar*":
            lines += ["            let text = if status.is_null() { String::new() } else { unsafe { std::ffi::CStr::from_ptr(status) }.to_string_lossy().into_owned() };",
                      "            Ok(NativeReply { status: 0, values: vec![Value::String(text)] })"]
        else:
            lines.append(f"            Ok(NativeReply {{ status: i64::from(status), values: vec![{', '.join(outputs)}] }})")
        lines.append("        }")
    lines += ['        _ => Err(Error::new("AttributeError", format!("Unlisted vendor export {symbol}"))),', "    }", "}", ""]
    return "\n".join(lines)


def generate(check=False):
    output = ROOT / "native" / "worker" / "src" / "generated"
    manifest = {}
    drift = []
    catalog = output / "error_codes.json"
    catalog_bytes = (ROOT / "amx_a/vendor/runtime/error_codes.json").read_bytes()
    if not catalog.is_file() or catalog.read_bytes() != catalog_bytes:
        drift.append(str(catalog.relative_to(ROOT)))
        if not check:
            output.mkdir(parents=True, exist_ok=True)
            catalog.write_bytes(catalog_bytes)
    for family, folder in FAMILIES.items():
        functions, metadata = declarations(family, folder)
        manifest[family] = {**metadata, "exports": functions}
        target = output / f"{family}.rs"
        content = rust_source(functions).encode()
        if not target.is_file() or target.read_bytes() != content:
            drift.append(str(target.relative_to(ROOT)))
            if not check:
                output.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
    target = output / "abi.json"
    content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    if not target.is_file() or target.read_bytes() != content:
        drift.append(str(target.relative_to(ROOT)))
        if not check:
            target.write_bytes(content)
    return drift


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    paths = generate(args.check)
    for path in paths:
        print(path)
    raise SystemExit(bool(args.check and paths))
