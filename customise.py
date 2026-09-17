#!/usr/bin/env python3
"""Customise this template into a new fileformats extension package.

Walks through the steps listed in the README, asking for the details it needs, and
then applies them: renames the placeholder packages, substitutes the placeholders
throughout the repository, writes out the format classes you describe along with
matching "extras" implementation stubs, and strips the instructions from the READMEs.

The available extras stubs aren't hard-coded: they are read off the base classes of
the namespace the formats belong to (e.g. ``fileformats.medimage``), so you are
offered exactly the hooks that namespace defines, with their real signatures.

Run it from the root of the repository::

    $ python3 customise.py

Answers can also be supplied non-interactively, which is useful for scripted
scaffolding and for testing this script::

    $ python3 customise.py --answers answers.json --no-input
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import keyword
import os
import re
import shutil
import subprocess
import sys
import tempfile
import typing as ty
import venv
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).parent.resolve()

# The repository being customised is on sys.path because this script lives in it, which
# puts its own (as yet unbuilt) fileformats packages into the namespace -- enough to
# break any import that walks it, such as resolving a mime-like. Nothing here is
# imported from the repository, so it is taken back off again.
sys.path[:] = [p for p in sys.path if p and Path(p).resolve() != REPO_ROOT]

PLACEHOLDER = "CHANGEME"
NAME_PLACEHOLDER = "<YOUR-NAME>"
EMAIL_PLACEHOLDER = "<YOUR-EMAIL>"
MIMELIKE_STEM = "MIMELIKE"
README_INSTRUCTIONS_END = "..."

#: Set in the environment of the re-executed script so it never bootstraps itself twice
BOOTSTRAP_ENV_VAR = "FILEFORMATS_CUSTOMISE_BOOTSTRAPPED"

#: Directories that are never scanned for placeholder substitution
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "build",
    "dist",
}

#: Extensions of files that are scanned for placeholder substitution
TEXT_SUFFIXES = {
    ".py",
    ".toml",
    ".cfg",
    ".ini",
    ".yml",
    ".yaml",
    ".md",
    ".rst",
    ".txt",
    ".flake8",
    "",
}

FILE = "file"
FILE_WITH_SIDE_CARS = "file-with-side-cars"
FILE_WITH_SEPARATE_HEADER = "file-with-separate-header"
DIRECTORY = "directory"

BINARY = "binary"
UNICODE = "unicode"

OPTION_DESCRIPTIONS = {
    FILE: "a single file",
    FILE_WITH_SIDE_CARS: "a file accompanied by side-car files (e.g. a JSON sidecar)",
    FILE_WITH_SEPARATE_HEADER: "a file whose metadata is in a separate header file",
    DIRECTORY: "a directory containing a set of files",
    BINARY: "contents are binary data",
    UNICODE: "contents are text",
}


class Abort(Exception):
    """Raised to stop the script cleanly with a message"""


# --------------------------------------------------------------------------------------
# Repository layout
# --------------------------------------------------------------------------------------


@dataclass
class Layout:
    """Where the placeholder packages live in this template.

    Both the plain and the vendor flavours of the template are supported, and which
    one this is gets detected from the directories that are present.
    """

    is_vendor: bool
    pkg_dir: Path
    extras_pkg_dir: Path

    @classmethod
    def detect(cls, root: Path) -> "Layout":
        vendor_pkg = root / "fileformats" / "vendor" / PLACEHOLDER
        plain_pkg = root / "fileformats" / PLACEHOLDER
        if vendor_pkg.is_dir():
            return cls(
                is_vendor=True,
                pkg_dir=vendor_pkg,
                extras_pkg_dir=root
                / "extras"
                / "fileformats"
                / "extras"
                / "vendor"
                / PLACEHOLDER,
            )
        if plain_pkg.is_dir():
            return cls(
                is_vendor=False,
                pkg_dir=plain_pkg,
                extras_pkg_dir=root / "extras" / "fileformats" / "extras" / PLACEHOLDER,
            )
        raise Abort(
            f"Couldn't find a '{PLACEHOLDER}' package under {root}. If this repository has "
            "already been customised there is nothing left for this script to do."
        )

    def renamed(self, name: str) -> ty.Tuple[Path, Path]:
        """The package directories renamed from the placeholder to `name`"""
        return (
            self.pkg_dir.with_name(name),
            self.extras_pkg_dir.with_name(name),
        )

    @property
    def module_path(self) -> str:
        return "fileformats.vendor" if self.is_vendor else "fileformats"

    @property
    def extras_module_path(self) -> str:
        return "fileformats.extras.vendor" if self.is_vendor else "fileformats.extras"


# --------------------------------------------------------------------------------------
# Prompting
# --------------------------------------------------------------------------------------


class Prompter:
    """Asks the questions, or takes the answers from a file instead.

    Parameters
    ----------
    answers : dict, optional
        pre-supplied answers, keyed by the question's key. Anything not present is
        asked for interactively unless `interactive` is False
    interactive : bool
        whether missing answers may be asked for
    """

    def __init__(
        self,
        answers: ty.Optional[ty.Dict[str, ty.Any]] = None,
        interactive: bool = True,
    ):
        self.answers = answers or {}
        self.interactive = interactive

    def _preset(self, key: str) -> ty.Any:
        return self.answers.get(key, None)

    def _read(self, prompt: str) -> str:
        if not self.interactive:
            raise Abort(f"No answer supplied for {prompt!r} and input is disabled")
        try:
            return input(prompt).strip()
        except EOFError:
            raise Abort("Input stream closed before all questions were answered")

    def text(
        self,
        key: str,
        question: str,
        default: ty.Optional[str] = None,
        validator: ty.Optional[ty.Callable[[str], str]] = None,
    ) -> str:
        preset = self._preset(key)
        if preset is not None:
            return validator(str(preset)) if validator else str(preset)
        suffix = f" [{default}]" if default else ""
        while True:
            answer = self._read(f"{question}{suffix}: ") or (default or "")
            if not answer:
                print("  A value is required")
                continue
            if validator is None:
                return answer
            try:
                return validator(answer)
            except ValueError as e:
                print(f"  {e}")

    def optional_text(
        self,
        key: str,
        question: str,
        validator: ty.Optional[ty.Callable[[str], str]] = None,
        blank_hint: str = "for none",
    ) -> ty.Optional[str]:
        preset = self._preset(key)
        if preset is not None:
            if not preset:
                return None
            return validator(str(preset)) if validator else str(preset)
        while True:
            answer = self._read(f"{question} (leave blank {blank_hint}): ")
            if not answer:
                return None
            if validator is None:
                return answer
            try:
                return validator(answer)
            except ValueError as e:
                print(f"  {e}")

    def choice(
        self, key: str, question: str, options: ty.Sequence[str], default: str
    ) -> str:
        preset = self._preset(key)
        if preset is not None:
            if preset not in options:
                raise Abort(
                    f"{preset!r} is not one of the valid answers for {key}: {list(options)}"
                )
            return str(preset)
        print(question)
        for i, option in enumerate(options, start=1):
            description = OPTION_DESCRIPTIONS.get(option, "")
            print(f"  {i}. {option}" + (f" - {description}" if description else ""))
        default_index = list(options).index(default) + 1
        while True:
            answer = self._read(f"Select 1-{len(options)} [{default_index}]: ")
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return options[int(answer) - 1]
            if answer in options:
                return answer
            print(f"  Please enter a number between 1 and {len(options)}")

    def yes_no(
        self,
        key: str,
        question: str,
        default: bool = True,
        explanation: ty.Optional[str] = None,
    ) -> bool:
        preset = self._preset(key)
        if preset is not None:
            return bool(preset)
        if explanation:
            print(explanation)
        suffix = " [Y/n]" if default else " [y/N]"
        while True:
            answer = self._read(f"{question}{suffix}: ").lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            print("  Please answer 'y' or 'n'")

    def multi(
        self,
        key: str,
        question: str,
        options: ty.Sequence[str],
        default: ty.Optional[ty.Sequence[str]] = None,
    ) -> ty.List[str]:
        """Select any number of `options`, by number, comma separated"""
        preset = self._preset(key)
        if preset is not None:
            unknown = set(preset) - set(options)
            if unknown:
                raise Abort(f"Unknown selections for {key}: {sorted(unknown)}")
            return list(preset)
        if not options:
            return []
        print(question)
        for i, option in enumerate(options, start=1):
            marker = " (default)" if default and option in default else ""
            print(f"  {i}. {option}{marker}")
        blank = "default" if default else "none"
        while True:
            answer = self._read(
                f"Select by number, comma separated ('all', or blank for {blank}): "
            )
            if not answer:
                return list(default) if default else []
            if answer.lower() == "all":
                return list(options)
            try:
                indices = [int(i) for i in answer.replace(" ", "").split(",") if i]
            except ValueError:
                print("  Please enter numbers separated by commas")
                continue
            if any(i < 1 or i > len(options) for i in indices):
                print(f"  Selections must be between 1 and {len(options)}")
                continue
            return [options[i - 1] for i in indices]


# --------------------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------------------


def valid_package_name(name: str) -> str:
    """A name that can be both a PyPI package suffix and a Python identifier"""
    name = name.strip().lower().replace("-", "_").replace(" ", "_")
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        raise ValueError(
            f"{name!r} must start with a letter and contain only letters, digits and underscores"
        )
    if keyword.iskeyword(name):
        raise ValueError(f"{name!r} is a Python keyword")
    return name


def valid_class_name(name: str) -> str:
    name = name.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name):
        raise ValueError(f"{name!r} must be a valid Python class name")
    return name[0].upper() + name[1:]


def valid_extension(ext: str) -> str:
    ext = ext.strip()
    if not ext.startswith("."):
        ext = "." + ext
    if len(ext) < 2:
        raise ValueError("An extension must have at least one character after the '.'")
    return ext


def snake_case(class_name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()


# --------------------------------------------------------------------------------------
# Extras hook detection
# --------------------------------------------------------------------------------------


@dataclass
class Hook:
    """An ``@extra`` hook that an extras package can provide an implementation for"""

    name: str
    declared_by: type
    method: ty.Callable[..., ty.Any]

    @property
    def label(self) -> str:
        return f"{self.name} (from {self.declared_by.__name__})"


def declares_extra(obj: ty.Any) -> bool:
    """Whether `obj` is a method decorated with fileformats' ``@extra``"""
    target = obj.__func__ if isinstance(obj, (classmethod, staticmethod)) else obj
    return hasattr(target, "_dispatch")


def collect_hooks(*classes: type) -> ty.Dict[str, Hook]:
    """Collect the ``@extra`` hooks declared across the MROs of `classes`.

    Parameters
    ----------
    *classes : type
        the classes the new formats will derive from

    Returns
    -------
    dict[str, Hook]
        the hooks that implementations can be registered against, keyed by name
    """
    hooks: ty.Dict[str, Hook] = {}
    for cls in classes:
        for klass in reversed(cls.__mro__):
            for name, attr in vars(klass).items():
                if name.startswith("_") or not declares_extra(attr):
                    continue
                target = (
                    attr.__func__
                    if isinstance(attr, (classmethod, staticmethod))
                    else attr
                )
                hooks[name] = Hook(name=name, declared_by=klass, method=target)
    return hooks


def import_namespace_bases(namespace: str) -> ty.List[type]:
    """Find the base classes of a fileformats namespace that declare ``@extra`` hooks.

    Parameters
    ----------
    namespace : str
        the fileformats namespace the new formats belong to, e.g. 'medimage'

    Returns
    -------
    list[type]
        the classes in that namespace that declare hooks, most general first
    """
    import importlib

    from fileformats.core import FileSet

    candidates: ty.List[type] = []
    for module_name in (f"fileformats.{namespace}.base", f"fileformats.{namespace}"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for obj in vars(module).values():
            if (
                inspect.isclass(obj)
                and issubclass(obj, FileSet)
                and obj not in candidates
                and obj.__module__.startswith(f"fileformats.{namespace}")
                and any(declares_extra(a) for a in vars(obj).values())
            ):
                candidates.append(obj)
        if candidates:
            break
    # most general first, so that the "marker" base classes are offered ahead of
    # the concrete formats that derive from them
    candidates.sort(key=lambda c: len(c.__mro__))
    return candidates


# --------------------------------------------------------------------------------------
# Format specifications
# --------------------------------------------------------------------------------------


@dataclass
class TypeRef:
    """A reference to the fileformats class of a side-car, header or directory member.

    It is either another format being defined in this run, in which case it is emitted
    into the same module, or an existing class resolved from its mime-like, in which
    case it is imported.
    """

    name: str
    import_from: ty.Optional[str] = None

    @property
    def is_local(self) -> bool:
        return self.import_from is None


@dataclass
class MemberSpec:
    """A file or directory that a directory format is required, or expected, to hold"""

    attr_name: str
    type_ref: TypeRef
    pattern: str
    is_glob: bool
    multiple: bool
    required: bool


@dataclass
class FormatSpec:
    """One format class to generate"""

    class_name: str
    kind: str
    docstring: str
    ext: ty.Optional[str] = None
    side_car_types: ty.List[TypeRef] = field(default_factory=list)
    header_type: ty.Optional[TypeRef] = None
    members: ty.List[MemberSpec] = field(default_factory=list)
    binary: bool = True
    has_magic_number: bool = False
    magic_number: ty.Optional[str] = None

    @property
    def var_name(self) -> str:
        return snake_case(self.class_name)

    @property
    def referenced_at_class_definition(self) -> ty.List[TypeRef]:
        """The types that need to have been defined before this class body is executed.

        Members are excluded: they are only resolved when the property is called, so
        they can refer to classes defined further down the module.
        """
        refs = list(self.side_car_types)
        if self.header_type:
            refs.append(self.header_type)
        refs.extend(m.type_ref for m in self.members)  # for content_types
        return refs


def resolve_mime_like(mime: str) -> TypeRef:
    """Resolve a mime-like string (e.g. 'application/json') to the class it names.

    Parameters
    ----------
    mime : str
        the mime-like (or mime) string to resolve

    Returns
    -------
    TypeRef
        a reference to the resolved class, importable from the shallowest package that
        re-exports it

    Raises
    ------
    ValueError
        if the string doesn't name a format that can be found
    """
    import importlib

    try:
        from fileformats.core import from_mime
    except ImportError:
        raise ValueError(
            "'fileformats' isn't installed, so mime-like types can't be resolved"
        )
    try:
        klass = from_mime(mime)
    except Exception as e:
        raise ValueError(f"Couldn't resolve {mime!r}: {e}")
    if not inspect.isclass(klass):
        raise ValueError(f"{mime!r} didn't resolve to a single format ({klass})")
    # prefer 'from fileformats.application import Json' over the module it is
    # implemented in, which is how they are referred to by hand
    parts = klass.__module__.split(".")
    for depth in range(2, len(parts) + 1):
        package = ".".join(parts[:depth])
        try:
            module = importlib.import_module(package)
        except ImportError:  # pragma: no cover - the class was imported from it
            continue
        if getattr(module, klass.__name__, None) is klass:
            return TypeRef(name=klass.__name__, import_from=package)
    return TypeRef(name=klass.__name__, import_from=klass.__module__)


def ask_type(
    prompter: Prompter,
    key: str,
    question: str,
    known: ty.Sequence[str],
    pending: ty.List[str],
    default: ty.Optional[str] = None,
) -> TypeRef:
    """Ask for the format of a side-car, header or directory member.

    The answer is one of the formats already described (given by name), a mime-like for
    a format that already exists (which is resolved, and re-asked if it doesn't), or the
    name of a new format, which is queued to be described once the formats already begun
    have been finished with.

    Parameters
    ----------
    prompter : Prompter
        the prompter to ask with
    key : str
        the key the answer is stored under
    question : str
        what is being asked for
    known : Sequence[str]
        the names of the formats described so far
    pending : list[str]
        names queued to be described later, appended to if a new name is given
    default : str, optional
        the answer taken if the question is just skipped over

    Returns
    -------
    TypeRef
        a reference to the format given
    """
    listed = ", ".join(known) if known else "none yet"
    while True:
        answer = prompter.text(
            key,
            f"{question}\n  (a mime-like, e.g. application/json; one of the formats "
            f"defined here: {listed};\n   or a new name to define afterwards)",
            default=default,
        )
        if answer in known or answer in pending:
            return TypeRef(name=answer)
        if "/" in answer:
            try:
                return resolve_mime_like(answer)
            except ValueError as e:
                print(f"  {e}")
                if not prompter.interactive:
                    raise Abort(str(e))
                continue
        try:
            class_name = valid_class_name(answer)
        except ValueError as e:
            print(f"  {e}")
            if not prompter.interactive:
                raise Abort(str(e))
            continue
        if class_name not in pending:
            pending.append(class_name)
            print(f"  '{class_name}' will be asked about once this format is done")
        return TypeRef(name=class_name)


def ask_members(
    prompter: Prompter,
    index: int,
    known: ty.Sequence[str],
    pending: ty.List[str],
) -> ty.List[MemberSpec]:
    """Ask for the contents a directory format holds.

    Each one becomes a property on the class, validated when the format is validated if
    it is required, so that a directory missing it isn't mistaken for this format.
    """
    members: ty.List[MemberSpec] = []
    if not prompter.yes_no(
        f"format_{index}_has_members",
        "Does it have contents that should be checked for, and accessible as properties?",
        default=False,
        explanation=(
            "  (e.g. a 'info_file' property for the 'info' file it must contain, which\n"
            "   makes a directory without one fail to validate as this format)"
        ),
    ):
        return members
    while True:
        position = len(members) + 1
        if members and not prompter.yes_no(
            f"format_{index}_member_{position}_another", "  Another?", default=False
        ):
            break
        attr_name = prompter.text(
            f"format_{index}_member_{position}_name",
            f"  Name of property {position} (e.g. info_file)",
            validator=valid_package_name,
        )
        pattern = prompter.text(
            f"format_{index}_member_{position}_pattern",
            "  File name, or a glob to match it by (e.g. info, or *.sfcm)",
        )
        type_ref = ask_type(
            prompter,
            f"format_{index}_member_{position}_type",
            "  Its format",
            known,
            pending,
        )
        is_glob = any(c in pattern for c in "*?[")
        multiple = (
            prompter.yes_no(
                f"format_{index}_member_{position}_multiple",
                "  Are there many of them (giving a dict keyed by file stem)?",
                default=False,
            )
            if is_glob
            else False
        )
        required = prompter.yes_no(
            f"format_{index}_member_{position}_required",
            "  Is it required (i.e. checked when the format is validated)?",
            default=True,
        )
        members.append(
            MemberSpec(
                attr_name=attr_name,
                type_ref=type_ref,
                pattern=pattern,
                is_glob=is_glob,
                multiple=multiple,
                required=required,
            )
        )
    return members


def ask_format(
    prompter: Prompter,
    index: int,
    known: ty.Sequence[str],
    pending: ty.List[str],
    class_name: ty.Optional[str] = None,
) -> FormatSpec:
    """Ask for one format.

    Parameters
    ----------
    prompter : Prompter
        the prompter to ask with
    index : int
        which format this is, used to key the answers
    known : Sequence[str]
        the names of the formats described so far
    pending : list[str]
        names queued to be described later, appended to by the type questions
    class_name : str, optional
        the name it was referred to by, when it was queued by an earlier format

    Returns
    -------
    FormatSpec
        the format as described
    """
    print(f"\n--- Format {index}{': ' + class_name if class_name else ''} ---")
    if class_name is None:
        class_name = prompter.text(
            f"format_{index}_class_name",
            "Class name of the format (e.g. VectraExport)",
            validator=valid_class_name,
        )
    kind = prompter.choice(
        f"format_{index}_kind",
        "What sort of data does it hold?",
        [FILE, FILE_WITH_SIDE_CARS, FILE_WITH_SEPARATE_HEADER, DIRECTORY],
        default=FILE,
    )
    docstring = prompter.text(
        f"format_{index}_docstring",
        "One-line description",
        default=f"{class_name} format",
    )

    ext = magic_number = None
    binary = True
    has_magic_number = False
    side_car_types: ty.List[TypeRef] = []
    header_type: ty.Optional[TypeRef] = None
    members: ty.List[MemberSpec] = []

    if kind != DIRECTORY:
        ext = prompter.text(
            f"format_{index}_ext",
            "File extension (e.g. .tom)",
            validator=valid_extension,
        )
        binary = (
            prompter.choice(
                f"format_{index}_binary",
                "Are the contents binary or text?",
                [BINARY, UNICODE],
                default=BINARY,
            )
            == BINARY
        )
        if binary:
            has_magic_number = prompter.yes_no(
                f"format_{index}_has_magic_number",
                "Does it have a magic number?",
                default=False,
                explanation=(
                    "  (a magic number is a fixed sequence of bytes at the start of a "
                    "file\n   that identifies its type, e.g. '%PDF-' for PDFs or "
                    "89504E47 for PNGs)"
                ),
            )
            if has_magic_number:
                magic_number = prompter.optional_text(
                    f"format_{index}_magic_number",
                    "  Magic number, in hex (e.g. 89504E47; needed for spaces and "
                    "non-printable bytes)\n  or as text (e.g. %PDF-)",
                    blank_hint="to fill in later",
                )

    if kind == FILE_WITH_SIDE_CARS:
        count = 1
        while True:
            side_car_types.append(
                ask_type(
                    prompter,
                    f"format_{index}_side_car_{count}_type",
                    f"  Format of side-car {count}",
                    known,
                    pending,
                    default="application/json",
                )
            )
            count += 1
            if not prompter.yes_no(
                f"format_{index}_side_car_{count}_another",
                "  Another side-car?",
                default=False,
            ):
                break
    elif kind == FILE_WITH_SEPARATE_HEADER:
        header_type = ask_type(
            prompter,
            f"format_{index}_header_type",
            "  Format of the header file",
            known,
            pending,
        )
    elif kind == DIRECTORY:
        members = ask_members(prompter, index, known, pending)

    return FormatSpec(
        class_name=class_name,
        kind=kind,
        docstring=docstring,
        ext=ext,
        side_car_types=side_car_types,
        header_type=header_type,
        members=members,
        binary=binary,
        has_magic_number=has_magic_number,
        magic_number=magic_number,
    )


def ask_formats(prompter: Prompter) -> ty.List[FormatSpec]:
    """Step through the formats the developer wants to define.

    Types referred to by a format's side-cars, header or contents that haven't been
    described yet are queued as they are named, and asked for once there are no more
    formats to begin, which in turn may queue more.
    """
    preset = prompter.answers.get("formats")
    if preset is not None:
        return [format_from_dict(f) for f in preset]
    formats: ty.List[FormatSpec] = []
    pending: ty.List[str] = []
    index = 0
    while True:
        index += 1
        if formats and not prompter.yes_no(
            f"_add_format_{index}", "\nDefine another format?", default=False
        ):
            break
        known = [f.class_name for f in formats]
        formats.append(ask_format(prompter, index, known, pending))
        if not prompter.interactive:
            break
    # the types that were referred to but not described, in the order they came up
    while True:
        described = {f.class_name for f in formats}
        outstanding = [n for n in pending if n not in described]
        if not outstanding:
            break
        index += 1
        known = [f.class_name for f in formats]
        formats.append(
            ask_format(prompter, index, known, pending, class_name=outstanding[0])
        )
    return formats


def format_from_dict(dct: ty.Dict[str, ty.Any]) -> FormatSpec:
    """Build a format from pre-supplied answers"""

    def as_type_ref(value: ty.Any) -> TypeRef:
        if isinstance(value, dict):
            return TypeRef(name=value["name"], import_from=value.get("import_from"))
        if "/" in value:
            return resolve_mime_like(value)
        return TypeRef(name=value)

    return FormatSpec(
        class_name=valid_class_name(dct["class_name"]),
        kind=dct["kind"],
        docstring=dct.get("docstring", ""),
        ext=valid_extension(dct["ext"]) if dct.get("ext") else None,
        side_car_types=[as_type_ref(v) for v in dct.get("side_car_types", [])],
        header_type=(
            as_type_ref(dct["header_type"]) if dct.get("header_type") else None
        ),
        members=[
            MemberSpec(
                attr_name=m["attr_name"],
                type_ref=as_type_ref(m["type"]),
                pattern=m["pattern"],
                is_glob=m.get("is_glob", any(c in m["pattern"] for c in "*?[")),
                multiple=m.get("multiple", False),
                required=m.get("required", True),
            )
            for m in dct.get("members", [])
        ],
        binary=dct.get("binary", True),
        has_magic_number=dct.get("has_magic_number", bool(dct.get("magic_number"))),
        magic_number=dct.get("magic_number") or None,
    )


# --------------------------------------------------------------------------------------
# Code generation
# --------------------------------------------------------------------------------------


def resolve_hints(method: ty.Callable[..., ty.Any]) -> ty.Dict[str, ty.Any]:
    """Resolve a hook's annotations to objects, so that they can be rendered against
    the imports the generated module makes, rather than being copied as the source
    text of a module with different imports"""
    try:
        return ty.get_type_hints(method)
    except Exception:  # pragma: no cover - unresolvable forward references
        return {}


def render_annotation(annotation: ty.Any, self_type: str) -> str:
    """Render a parameter/return annotation as source text.

    Rendered so that it resolves against the imports the generated module makes:
    `repr` is used for typing constructs and unions, since it keeps the qualifying
    module (unlike ``inspect.formatannotation``, which strips 'typing.'), and the
    module-qualified name is used for plain classes.

    ``Self`` is replaced by `self_type`, since the generated implementations are
    module-level functions, where ``Self`` isn't valid.
    """
    if annotation is inspect.Parameter.empty:
        return ""
    if isinstance(annotation, str):
        rendered = annotation
    elif isinstance(annotation, type):
        if annotation.__module__ in ("builtins", None):
            rendered = annotation.__qualname__
        else:
            rendered = f"{annotation.__module__}.{annotation.__qualname__}"
    else:
        rendered = repr(annotation)
    rendered = re.sub(r"\b(?:ty|typing|typing_extensions)\.Self\b", self_type, rendered)
    if rendered == "Self":
        rendered = self_type
    return rendered


def render_extra_stub(hook: Hook, fmt: FormatSpec) -> str:
    """Generate an implementation stub for `hook` dispatching on `fmt`.

    The signature is taken from the hook itself so that it matches what
    ``extra_implementation`` validates it against, with the first parameter
    re-annotated as the format being implemented for.
    """
    signature = inspect.signature(hook.method)
    hints = resolve_hints(hook.method)
    parameters = list(signature.parameters.values())
    rendered_params = [f"{fmt.var_name}: {fmt.class_name}"]
    for param in parameters[1:]:
        text = param.name
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            text = "*" + text
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            text = "**" + text
        annotation = render_annotation(
            hints.get(param.name, param.annotation), fmt.class_name
        )
        if annotation:
            text += f": {annotation}"
        if param.default is not inspect.Parameter.empty:
            text += f" = {param.default!r}"
        rendered_params.append(text)
    returns = render_annotation(
        hints.get("return", signature.return_annotation), fmt.class_name
    )
    returns = f" -> {returns}" if returns else ""
    params_block = ",\n    ".join(rendered_params)
    return (
        f"@extra_implementation({hook.declared_by.__name__}.{hook.name})\n"
        f"def {fmt.var_name}_{hook.name}(\n    {params_block},\n){returns}:\n"
        f'    """TODO: implement the \'{hook.name}\' extra for {fmt.class_name}"""\n'
        f"    raise NotImplementedError\n"
    )


def render_magic_number(value: str) -> str:
    """Render a magic number as source text.

    Both of the forms `WithMagicNumber` accepts are used in the wild, so a value that
    reads as hex is rendered as the hex string it documents, and anything else as a byte
    string. Whitespace is only insignificant in the hex form, so the text form is taken
    exactly as given, with anything unprintable escaped.

    Parameters
    ----------
    value : str
        the magic number as given, in hex (e.g. '89504E47', or '89 50 4E 47') or as the
        text it spells out (e.g. '%PDF-')

    Returns
    -------
    str
        the value as it should appear in the class body
    """
    compact = value.strip().replace(" ", "")
    if compact.lower().startswith("0x"):
        compact = compact[2:]
    try:
        bytes.fromhex(compact)
    except ValueError:
        pass
    else:
        return f'"{compact.upper()}"'
    literal = ""
    for byte in value.encode():
        char = chr(byte)
        if char in '"\\':
            literal += "\\" + char
        elif 32 <= byte < 127:
            literal += char
        else:
            literal += f"\\x{byte:02x}"
    return f'b"{literal}"'


def order_by_dependency(formats: ty.List[FormatSpec]) -> ty.List[FormatSpec]:
    """Order the formats so that each is defined after the ones its class body names.

    Side-car, header and content types are looked up when the class body is executed,
    so they have to appear earlier in the module.
    """
    by_name = {f.class_name: f for f in formats}
    ordered: ty.List[FormatSpec] = []
    placed: ty.Set[str] = set()
    placing: ty.Set[str] = set()

    def place(fmt: FormatSpec) -> None:
        if fmt.class_name in placed or fmt.class_name in placing:
            return  # already emitted, or a cycle that can't be resolved by ordering
        placing.add(fmt.class_name)
        for ref in fmt.referenced_at_class_definition:
            if ref.is_local and ref.name in by_name:
                place(by_name[ref.name])
        placing.discard(fmt.class_name)
        placed.add(fmt.class_name)
        ordered.append(fmt)

    for fmt in formats:
        place(fmt)
    return ordered


def render_member(member: MemberSpec, owner: FormatSpec) -> ty.List[str]:
    """Render a directory member as a property of the class.

    Required members are validated properties, so that a directory without them doesn't
    validate as the format; optional ones are plain properties returning None when
    absent, as their absence shouldn't fail validation.
    """
    tp = member.type_ref.name
    lines = []
    if member.multiple:
        returns = f"dict[str, {tp}]"
        lines.append("    @validated_property" if member.required else "    @property")
        lines.append(f"    def {member.attr_name}(self) -> {returns}:")
        lines.append(
            f'        matches = {{p.stem: {tp}(p) for p in self.fspath.glob("{member.pattern}")}}'
        )
        if member.required:
            lines.append("        if not matches:")
            lines.append("            raise FormatMismatchError(")
            lines.append(
                f'                f"Did not find any {member.pattern} in {{self.fspath}}"'
            )
            lines.append("            )")
        lines.append("        return matches")
    elif member.is_glob:
        returns = tp if member.required else f"{tp} | None"
        lines.append("    @validated_property" if member.required else "    @property")
        lines.append(f"    def {member.attr_name}(self) -> {returns}:")
        lines.append(f'        matches = list(self.fspath.glob("{member.pattern}"))')
        if member.required:
            lines.append("        if not matches:")
            lines.append("            raise FormatMismatchError(")
            lines.append(
                f'                f"Did not find a {member.pattern} in {{self.fspath}}"'
            )
            lines.append("            )")
            lines.append(f"        return {tp}(matches[0])")
        else:
            lines.append(f"        return {tp}(matches[0]) if matches else None")
    else:
        if member.required:
            lines.append("    @validated_property")
            lines.append(f"    def {member.attr_name}(self) -> {tp}:")
            lines.append(f'        return {tp}(self.fspath / "{member.pattern}")')
        else:
            lines.append("    @property")
            lines.append(f"    def {member.attr_name}(self) -> {tp} | None:")
            lines.append(f'        path = self.fspath / "{member.pattern}"')
            lines.append(f"        return {tp}(path) if path.exists() else None")
    return lines


def render_formats_module(
    formats: ty.List[FormatSpec], namespace_bases: ty.List[str]
) -> str:
    """Generate the module defining the format classes"""
    formats = order_by_dependency(formats)
    generic_imports = set()
    mixin_imports = set()
    core_imports = set()
    external_imports: ty.Dict[str, ty.Set[str]] = {}
    needs_mismatch_error = False

    def note(ref: TypeRef) -> None:
        if not ref.is_local:
            external_imports.setdefault(str(ref.import_from), set()).add(ref.name)

    for fmt in formats:
        if fmt.kind == DIRECTORY:
            # content_types are only checked by TypedDirectory; on a plain Directory
            # they are inert metadata
            generic_imports.add("TypedDirectory" if fmt.members else "Directory")
        else:
            generic_imports.add("BinaryFile" if fmt.binary else "UnicodeFile")
        if fmt.kind == FILE_WITH_SIDE_CARS:
            mixin_imports.add("WithSideCars")
        if fmt.kind == FILE_WITH_SEPARATE_HEADER:
            mixin_imports.add("WithSeparateHeader")
        if fmt.has_magic_number:
            mixin_imports.add("WithMagicNumber")
        for ref in fmt.side_car_types:
            note(ref)
        if fmt.header_type:
            note(fmt.header_type)
        for member in fmt.members:
            note(member.type_ref)
            core_imports.add("validated_property" if member.required else "")
            if member.required:
                needs_mismatch_error = True
    core_imports.discard("")

    lines = []
    if core_imports:
        lines.append(f"from fileformats.core import {', '.join(sorted(core_imports))}")
    if needs_mismatch_error:
        lines.append("from fileformats.core.exceptions import FormatMismatchError")
    if mixin_imports:
        lines.append(
            f"from fileformats.core.mixin import {', '.join(sorted(mixin_imports))}"
        )
    if generic_imports:
        lines.append(
            f"from fileformats.generic import {', '.join(sorted(generic_imports))}"
        )
    for module in sorted(external_imports):
        lines.append(
            f"from {module} import {', '.join(sorted(external_imports[module]))}"
        )
    lines.extend(namespace_bases)

    body = ["\n".join(lines), "", ""]
    for fmt in formats:
        bases = []
        if fmt.has_magic_number:
            bases.append("WithMagicNumber")
        if fmt.kind == FILE_WITH_SIDE_CARS:
            bases.append("WithSideCars")
        if fmt.kind == FILE_WITH_SEPARATE_HEADER:
            bases.append("WithSeparateHeader")
        if fmt.kind == DIRECTORY:
            bases.append("TypedDirectory" if fmt.members else "Directory")
        else:
            bases.append("BinaryFile" if fmt.binary else "UnicodeFile")
        bases.extend(base_name_of(imp) for imp in namespace_bases)
        body.append(f"class {fmt.class_name}({', '.join(bases)}):")
        body.append(f'    """{fmt.docstring}"""')
        body.append("")
        declared = False
        if fmt.ext:
            body.append(f'    ext = "{fmt.ext}"')
            declared = True
        if fmt.has_magic_number:
            if fmt.magic_number:
                body.append(
                    f"    magic_number = {render_magic_number(fmt.magic_number)}"
                )
            else:
                body.append(
                    '    # TODO: the magic number, in hex (e.g. "89504E47") or as a'
                )
                body.append('    # byte string (e.g. b"%PDF-"). NB: until it is filled')
                body.append("    # in, no file will validate as this format")
                body.append('    magic_number = ""')
            declared = True
        if fmt.side_car_types:
            names = ", ".join(r.name for r in fmt.side_car_types)
            suffix = "," if len(fmt.side_car_types) == 1 else ""
            body.append(f"    side_car_types = ({names}{suffix})")
            declared = True
        if fmt.header_type:
            body.append(f"    header_type = {fmt.header_type.name}")
            declared = True
        if fmt.members:
            content = sorted({m.type_ref.name for m in fmt.members})
            suffix = "," if len(content) == 1 else ""
            body.append(f"    content_types = ({', '.join(content)}{suffix})")
            declared = True
            for member in fmt.members:
                body.append("")
                body.extend(render_member(member, fmt))
        if not declared:
            body.append("    pass")
        body.extend(["", ""])
    return "\n".join(body).rstrip() + "\n"


def base_name_of(import_line: str) -> str:
    """The class name imported by a 'from x import Y' line"""
    return import_line.rsplit(" import ", 1)[1]


def render_extras_module(
    formats: ty.List[FormatSpec],
    hooks: ty.List[Hook],
    formats_module: str,
) -> str:
    """Generate the extras module with a stub per format per selected hook"""
    if not hooks:
        return (
            "# Implement 'extras' for the formats defined in\n"
            f"# {formats_module} here, see\n"
            "# https://arcanaframework.github.io/fileformats/developer/extras.html\n"
        )
    header = [
        "import os",
        "import pathlib",
        "import typing",
        "",
        "from fileformats.core import extra_implementation",
    ]
    declaring = sorted({h.declared_by for h in hooks}, key=lambda c: c.__name__)
    for cls in declaring:
        header.append(f"from {cls.__module__} import {cls.__name__}")
    imported = ", ".join(f.class_name for f in formats)
    header.append(f"from {formats_module} import {imported}")
    blocks = ["\n".join(header), "", ""]
    for fmt in formats:
        for hook in hooks:
            blocks.append(render_extra_stub(hook, fmt))
            blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def render_base_module(class_name: str, docstring: str) -> str:
    """Generate the module defining the namespace's own base class.

    An extension package defines a namespace, i.e. a qualitative type of data, and the
    base class is what says so: the formats in the package derive from it, and any
    'extras' hooks particular to that type of data are declared on it.

    Parameters
    ----------
    class_name : str
        the name of the base class (e.g. 'Biosig')
    docstring : str
        what the namespace covers (e.g. 'Base class for biophysical recordings')

    Returns
    -------
    str
        the contents of the base module
    """
    return f'''import os  # noqa: F401
import typing as ty  # noqa: F401

from fileformats.core import FileSet, extra  # noqa: F401


class {class_name}(FileSet):
    """{docstring}"""

    # Hooks declared here are the ones that make sense for this type of data, and are
    # implemented in the 'extras' package for each format that supports them, e.g.
    #
    #     @extra
    #     def deidentify(
    #         self,
    #         out_dir: os.PathLike[str],
    #         spec: ty.Any = None,
    #         **kwargs: ty.Any,
    #     ) -> ty.Self:
    #         """Strip any identifying information from the data"""
'''


def render_init(
    module_name: str,
    formats: ty.List[FormatSpec],
    base_class: ty.Optional[str] = None,
) -> str:
    """Generate the package __init__, re-exporting the base and format classes"""
    names = [f.class_name for f in formats]
    lines = ["from ._version import __version__"]
    if base_class:
        lines.append(f"from .base import {base_class}")
    if names:
        lines.append(f"from .{module_name} import (")
        lines.extend(f"    {n}," for n in names)
        lines.append(")")
    lines.append("")
    lines.append("__all__ = [")
    lines.append('    "__version__",')
    if base_class:
        lines.append(f'    "{base_class}",')
    lines.extend(f'    "{n}",' for n in names)
    lines.append("]")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Bootstrapping
# --------------------------------------------------------------------------------------


def fileformats_available() -> bool:
    """Whether the fileformats package can be imported in this interpreter"""
    try:
        return importlib.util.find_spec("fileformats.core") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken namespace package
        return False


def bootstrap_into_venv(
    namespace: ty.Optional[str],
    answers: ty.Dict[str, ty.Any],
    keep_script: bool,
    root: Path,
) -> int:
    """Re-run this script in a throwaway virtual environment with fileformats installed.

    The hooks on offer are read off the installed fileformats packages, so without them
    only the renaming steps can be performed. Rather than requiring the developer to
    install anything into their own environment before customising the template, a
    temporary environment is built for the purpose and thrown away afterwards.

    The answers given so far are passed on to the re-executed script, so that nothing is
    asked twice, and it is left interactive for the questions that remain.

    Parameters
    ----------
    namespace : str, optional
        the namespace whose package should be installed alongside fileformats, so that
        the hooks it declares can be detected
    answers : dict
        the answers collected before bootstrapping
    keep_script : bool
        whether the re-executed script should keep itself afterwards
    root : Path
        the repository being customised

    Returns
    -------
    int
        the exit status of the re-executed script
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="fileformats-customise-"))
    try:
        env_dir = tmp_dir / "venv"
        print(f"\nBuilding a temporary environment in {env_dir} ...")
        venv.create(env_dir, with_pip=True)
        bin_dir = "Scripts" if os.name == "nt" else "bin"
        python = env_dir / bin_dir / ("python.exe" if os.name == "nt" else "python")

        packages = ["fileformats"]
        if namespace:
            packages.append(f"fileformats-{namespace.replace('_', '-')}")
        print(f"Installing {', '.join(packages)} ...")
        installed = subprocess.run(
            [str(python), "-m", "pip", "install", "--quiet", *packages],
            capture_output=True,
            text=True,
        )
        if installed.returncode != 0 and namespace:
            # the namespace may not be published (yet), so fall back to core alone
            print(
                f"  Couldn't install 'fileformats-{namespace}' "
                "-- continuing with the hooks every file format has"
            )
            installed = subprocess.run(
                [str(python), "-m", "pip", "install", "--quiet", "fileformats"],
                capture_output=True,
                text=True,
            )
        if installed.returncode != 0:
            print(f"  Installation failed:\n{installed.stderr.strip()}")
            return 1

        answers_file = tmp_dir / "answers.json"
        answers_file.write_text(json.dumps(answers))
        argv = [
            str(python),
            str(Path(__file__).resolve()),
            "--answers",
            str(answers_file),
            "--root",
            str(root),
        ]
        if keep_script:
            argv.append("--keep-script")
        print("Re-running in the temporary environment ...\n")
        return subprocess.run(
            argv, env={**os.environ, BOOTSTRAP_ENV_VAR: "1"}
        ).returncode
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# --------------------------------------------------------------------------------------
# Applying the changes
# --------------------------------------------------------------------------------------


def in_git_repo(root: Path) -> bool:
    return (root / ".git").exists()


def move(src: Path, dest: Path, root: Path) -> None:
    """Move `src` to `dest`, keeping git aware of the rename where possible"""
    if src == dest:
        return
    if in_git_repo(root):
        try:
            result = subprocess.run(
                ["git", "mv", str(src.relative_to(root)), str(dest.relative_to(root))],
                cwd=root,
                capture_output=True,
                text=True,
            )
        except OSError:  # git isn't installed, or isn't on the PATH
            result = None
        if result is not None and result.returncode == 0:
            return
    shutil.move(str(src), str(dest))


def iter_text_files(root: Path) -> ty.Iterator[Path]:
    this_script = Path(__file__).resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == this_script:
            continue  # the placeholders are defined in here as literals
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name.startswith("."):
            yield path


def replace_in_tree(root: Path, replacements: ty.Dict[str, str]) -> ty.List[Path]:
    """Apply `replacements` to every text file under `root`"""
    changed = []
    for path in iter_text_files(root):
        try:
            content = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        updated = content
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != content:
            path.write_text(updated)
            changed.append(path)
    return changed


def strip_readme_instructions(path: Path) -> bool:
    """Drop the "How to customise this template" preamble from a README"""
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.strip() == README_INSTRUCTIONS_END:
            remaining = lines[i + 1 :]
            while remaining and not remaining[0].strip():
                remaining.pop(0)
            path.write_text("\n".join(remaining) + "\n")
            return True
    return False


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def parse_args(argv: ty.Optional[ty.Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--answers",
        type=Path,
        default=None,
        help="JSON file of answers, so the script can be run without prompting",
    )
    parser.add_argument(
        "--no-input",
        action="store_true",
        help="fail rather than prompt for anything missing from --answers",
    )
    parser.add_argument(
        "--keep-script",
        action="store_true",
        help="keep customise.py afterwards instead of offering to delete it",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="the repository to customise (defaults to the one this script is in)",
    )
    return parser.parse_args(argv)


def main(argv: ty.Optional[ty.Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    answers = json.loads(args.answers.read_text()) if args.answers else {}
    prompter = Prompter(answers, interactive=not args.no_input)

    try:
        layout = Layout.detect(root)
    except Abort as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(
        f"Customising the {'vendor ' if layout.is_vendor else ''}fileformats extension "
        f"template in {root}\n"
    )

    try:
        # ---- package identity -------------------------------------------------------
        if layout.is_vendor:
            vendor = prompter.text(
                "vendor_name",
                "What is the name of the vendor (e.g. canfield)",
                validator=valid_package_name,
            )
            pkg_name = vendor
            distribution = f"fileformats-vendor-{vendor.replace('_', '-')}"
            namespace: ty.Optional[str] = prompter.text(
                "namespace",
                "Which fileformats namespace will the formats be part of "
                "(e.g. medimage, biosig, datascience)",
                validator=valid_package_name,
            )
        else:
            pkg_name = prompter.text(
                "extension_name",
                "What is the name of the extension package (e.g. biosig)",
                validator=valid_package_name,
            )
            distribution = f"fileformats-{pkg_name.replace('_', '-')}"
            # an extension package *is* a namespace -- it defines a qualitative type of
            # data (e.g. 'biosig' for biophysical recordings) rather than sitting within
            # a namespace someone else defined, which is what the vendor template is for
            namespace = None
        author = prompter.text("author_name", "Your name (for pyproject.toml)")
        email = prompter.text("author_email", "Your email address")

        # ---- the namespace's own base class ------------------------------------------
        base_class: ty.Optional[str] = None
        base_class_docstring = ""
        if not layout.is_vendor:
            if prompter.yes_no(
                "has_base_class",
                f"Define a base class for the '{pkg_name}' namespace?",
                default=True,
                explanation=(
                    "  (a namespace covers a qualitative type of data, and its base "
                    "class is what\n   says so: the formats derive from it, and any "
                    "'extras' hooks particular to\n   that type of data are declared "
                    "on it)"
                ),
            ):
                base_class = prompter.text(
                    "base_class_name",
                    "  Name of the base class",
                    default=pkg_name[0].upper() + pkg_name[1:],
                    validator=valid_class_name,
                )
                base_class_docstring = prompter.text(
                    "base_class_docstring",
                    "  What the namespace covers",
                    default=f"Base class for {pkg_name} data",
                )

        # ---- make sure the hooks can be detected ------------------------------------
        if not fileformats_available() and not os.environ.get(BOOTSTRAP_ENV_VAR):
            print(
                "\n'fileformats' isn't installed in this environment, so the 'extras' "
                "hooks\navailable to the formats can't be detected."
            )
            if prompter.yes_no(
                "bootstrap",
                "Build a temporary virtual environment to detect them in "
                "(needs network access)",
                default=True,
            ):
                collected = {
                    "author_name": author,
                    "author_email": email,
                    "namespace": namespace or "",
                }
                if layout.is_vendor:
                    collected["vendor_name"] = pkg_name
                else:
                    collected["extension_name"] = pkg_name
                collected.update(
                    {k: v for k, v in answers.items() if k not in collected}
                )
                return bootstrap_into_venv(namespace, collected, args.keep_script, root)

        # ---- the namespace's extras hooks -------------------------------------------
        namespace_bases: ty.List[type] = []
        if namespace and fileformats_available():
            try:
                namespace_bases = import_namespace_bases(namespace)
            except Exception as e:  # pragma: no cover - depends on what is installed
                print(f"\nWarning: couldn't inspect the '{namespace}' namespace ({e}).")
        if not fileformats_available():
            print(
                "\nContinuing without 'extras' stubs, as 'fileformats' isn't installed."
            )
        elif namespace and not namespace_bases:
            print(
                f"\nNote: no base classes declaring 'extras' hooks were found in "
                f"fileformats.{namespace} -- is 'fileformats-{namespace}' installed? "
                "Falling back to the hooks every file format has."
            )
        if not namespace_bases and fileformats_available():
            from fileformats.core import FileSet

            namespace_bases = [FileSet]

        base_options = [f"{c.__module__}.{c.__name__}" for c in namespace_bases]
        selected_base_names = (
            prompter.multi(
                "base_classes",
                "\nWhich base classes should the formats derive from?",
                base_options,
                default=base_options[:1],
            )
            if base_options and not base_class
            else base_options[:1]
        )
        selected_bases = [
            c
            for c in namespace_bases
            if f"{c.__module__}.{c.__name__}" in selected_base_names
        ]
        # FileSet is never listed as an explicit base, since the generic file and
        # directory classes the formats derive from are subclasses of it already. Its
        # hooks are still offered, being picked up from their MRO
        format_bases = [c for c in selected_bases if c.__name__ != "FileSet"]
        base_imports = [
            f"from {c.__module__} import {c.__name__}" for c in format_bases
        ]
        if base_class:
            # the namespace's own base class, which lives alongside the formats
            base_imports.append(f"from .base import {base_class}")

        # ---- the formats ------------------------------------------------------------
        print(
            "\nNow describe the formats to define (you can always add more by hand later)."
        )
        formats = ask_formats(prompter)
        if not formats:
            print("\nNo formats described, only the renaming steps will be applied.")

        # ---- the extras stubs -------------------------------------------------------
        hooks_by_name = collect_hooks(*selected_bases) if selected_bases else {}
        chosen_hooks: ty.List[Hook] = []
        if formats and hooks_by_name:
            labels = [h.label for h in hooks_by_name.values()]
            chosen_labels = prompter.multi(
                "extras",
                "\nWhich 'extras' implementation stubs should be added for each format?",
                labels,
            )
            chosen_hooks = [
                h for h in hooks_by_name.values() if h.label in chosen_labels
            ]

        # ---- confirm ----------------------------------------------------------------
        module_name = namespace if (layout.is_vendor and namespace) else pkg_name
        print("\n" + "=" * 70)
        print(f"  distribution:  {distribution}")
        print(f"  package:       {layout.module_path}.{pkg_name}")
        print(f"  extras:        {layout.extras_module_path}.{pkg_name}")
        print(f"  formats module: {module_name}.py")
        if base_class:
            print(f"  base class:    {base_class} (in base.py)")
        print(f"  author:        {author} <{email}>")
        for fmt in formats:
            detail = fmt.ext or ", ".join(
                sorted({m.type_ref.name for m in fmt.members})
            )
            described = fmt.kind if fmt.binary else f"text {fmt.kind}"
            if fmt.has_magic_number:
                detail += f", magic {fmt.magic_number or 'TBC'}"
            if fmt.side_car_types:
                detail += " + side-cars " + ", ".join(
                    r.name for r in fmt.side_car_types
                )
            if fmt.header_type:
                detail += f" + {fmt.header_type.name} header"
            print(
                f"    - {fmt.class_name} ({described}{': ' + detail if detail else ''})"
            )
        if chosen_hooks:
            print(f"  extras stubs:  {', '.join(h.name for h in chosen_hooks)}")
        print("=" * 70)
        if not prompter.yes_no("confirm", "\nApply these changes?", default=True):
            print("Nothing changed.")
            return 1

        # ---- apply ------------------------------------------------------------------
        pkg_dir, extras_pkg_dir = layout.renamed(pkg_name)
        move(layout.pkg_dir, pkg_dir, root)
        move(layout.extras_pkg_dir, extras_pkg_dir, root)

        for directory in (pkg_dir, extras_pkg_dir):
            stub = directory / f"{MIMELIKE_STEM}.py"
            if stub.exists():
                move(stub, directory / f"{module_name}.py", root)

        replace_in_tree(
            root,
            {PLACEHOLDER: pkg_name, NAME_PLACEHOLDER: author, EMAIL_PLACEHOLDER: email},
        )

        if base_class:
            (pkg_dir / "base.py").write_text(
                render_base_module(base_class, base_class_docstring)
            )
        if formats:
            formats_module = pkg_dir / f"{module_name}.py"
            formats_module.write_text(render_formats_module(formats, base_imports))
            (pkg_dir / "__init__.py").write_text(
                render_init(module_name, formats, base_class)
            )
            extras_module = extras_pkg_dir / f"{module_name}.py"
            extras_module.write_text(
                render_extras_module(
                    formats,
                    chosen_hooks,
                    f"{layout.module_path}.{pkg_name}.{module_name}",
                )
            )
            extras_init = extras_pkg_dir / "__init__.py"
            extras_init.write_text(
                extras_init.read_text().rstrip("\n")
                + f"\nfrom . import {module_name}  # noqa: F401\n"
            )

        for readme in (root / "README.md", root / "extras" / "README.md"):
            strip_readme_instructions(readme)

        print("\nDone. Next steps:")
        print("  1. pip install -e .[test] -e ./extras[test]")
        print("  2. Fill in the format classes and the 'extras' stubs")
        print("  3. pytest")

        if not args.keep_script and prompter.yes_no(
            "delete_script",
            "\nDelete customise.py now that it has been run?",
            default=True,
        ):
            script = Path(__file__).resolve()
            if in_git_repo(root):
                subprocess.run(
                    ["git", "rm", "-q", "--cached", script.name],
                    cwd=root,
                    capture_output=True,
                )
            script.unlink()
            print("Removed customise.py")
    except Abort as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
