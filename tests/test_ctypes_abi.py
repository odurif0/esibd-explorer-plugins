"""Compare the declared ctypes ABI directly with the bundled vendor headers."""

import ast
import ctypes
from ctypes import wintypes
import importlib.util
import inspect
import re
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASES = sorted(ROOT.glob("*/vendor/runtime/*/*_base.py"))


def header_signatures(path):
    # These headers use only scalar types, C++ references and fixed arrays.
    # Reject anything outside that small grammar instead of guessing an ABI.
    text = re.sub(r"/\*.*?\*/|//[^\n]*", "", path.read_text(), flags=re.S)
    text = re.sub(r"\b(?:FAR|_export)\b", "", text)
    functions = {}
    for match in re.finditer(
        r"^[ \t#]*(WORD|int|const\s+char\s*\*)\s*(COM_\w+)\s*\(([^;]*)\);",
        text, re.M,
    ):
        result, name, arguments = match.groups()
        result = re.sub(r"\s+", "", result)
        params = []
        for argument in arguments.split(",") if arguments.strip() else []:
            argument = re.sub(r"\bconst\b", "", argument)
            parsed = re.fullmatch(
                r"\s*(unsigned|WORD|DWORD|BYTE|BOOL|bool|double|float|int|char)"
                r"\s*([&*]?)\s*(\w+)\s*(\[[^\]]+\])?\s*",
                argument,
            )
            assert parsed, f"Unsupported C declaration: {path}: {argument}"
            scalar, reference, _param, array = parsed.groups()
            params.append(scalar + ("*" if reference or array else ""))
        signature = (params, result)
        if name in functions:
            assert functions[name] == signature, f"Conflicting declarations for {name}"
        functions[name] = signature
    assert functions, f"No exports found in {path}"
    return functions


def native_names(path):
    tree = ast.parse(path.read_text())
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr.startswith("COM_")}


def ctypes_type(c_type):
    scalar_types = {
        "BYTE": ctypes.c_ubyte, "WORD": ctypes.c_uint16, "DWORD": ctypes.c_uint32,
        "unsigned": ctypes.c_uint, "BOOL": wintypes.BOOL, "bool": ctypes.c_bool,
        "double": ctypes.c_double, "float": ctypes.c_float, "int": ctypes.c_int,
        "char": ctypes.c_char,
    }
    if c_type in {"char*", "constchar*"}:
        return ctypes.c_char_p
    if c_type.endswith("*"):
        return ctypes.POINTER(scalar_types[c_type[:-1]])
    return scalar_types[c_type]


def load_base(path):
    spec = importlib.util.spec_from_file_location("abi_base_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = next(value for name, value in vars(module).items() if name.endswith("Base") and isinstance(value, type))
    return cls.__new__(cls)


def dll_attribute(path):
    tree = ast.parse(path.read_text())
    return next(
        node.value.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr.startswith("COM_")
        and isinstance(node.value, ast.Attribute)
    )


@pytest.mark.parametrize("path", BASES, ids=lambda path: path.parents[3].name)
def test_all_used_exports_have_the_vendor_signature(path):
    header = next((path.parent / "vendor").glob("*.h"))
    declarations = header_signatures(header)
    used = native_names(path)
    assert used <= declarations.keys(), f"Undocumented exports: {used - declarations.keys()}"
    driver = load_base(path)
    functions = {name: types.SimpleNamespace(argtypes=None, restype=None) for name in used}
    setattr(driver, dll_attribute(path), types.SimpleNamespace(**functions))
    driver._configure_dll_signatures()
    for name, function in functions.items():
        arguments, result = declarations[name]
        assert function.argtypes == [ctypes_type(value) for value in arguments], name
        assert function.restype is ctypes_type(result), name


@pytest.mark.parametrize("path", BASES, ids=lambda path: path.parents[3].name)
def test_getters_pass_arguments_accepted_by_ctypes(path):
    declarations = header_signatures(next((path.parent / "vendor").glob("*.h")))
    calls = []
    failures = []

    class NativeFunction:
        def __init__(self, name):
            self.name = name

        def __call__(self, *args):
            # Use ctypes' real argument converters, not permissive Python stubs.
            arguments, _result = declarations[self.name]
            try:
                assert len(args) == len(arguments), self.name
                for c_type, value in zip(arguments, args):
                    ctypes_type(c_type).from_param(value)
            except Exception as exc:
                failures.append((self.name, str(exc)))
                raise
            calls.append(self.name)
            return b"OK" if self.restype is ctypes.c_char_p else 0

    driver = load_base(path)
    driver.port = driver.stream = 0
    setattr(driver, dll_attribute(path), types.SimpleNamespace(**{
        name: NativeFunction(name) for name in native_names(path)
    }))
    driver._configure_dll_signatures()
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("get_"):
            continue
        if not any(isinstance(child, ast.Attribute) and child.attr.startswith("COM_") for child in ast.walk(node)):
            continue
        method = getattr(driver, node.name)
        kwargs = {
            name: 1 for name, param in inspect.signature(method).parameters.items()
            if param.default is inspect.Parameter.empty
        }
        method(**kwargs)
    assert calls
    assert not failures, failures


@pytest.mark.parametrize("path", BASES, ids=lambda path: path.parents[3].name)
def test_signature_setup_does_not_require_optional_exports(path):
    driver = load_base(path)
    setattr(driver, dll_attribute(path), types.SimpleNamespace())
    driver._configure_dll_signatures()
