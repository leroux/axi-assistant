"""Custom lint rules for the axi-assistant codebase."""

import libcst as cst
from fixit import Invalid, LintRule, Valid


class NoBotImports(LintRule):
    """Flag imports from main.py — it is the top-level orchestrator.

    main.py sits at the top of the dependency DAG and should never be imported
    by library modules. Allowing such imports would create circular dependencies.
    """

    VALID = [
        Valid("from axi.agents import spawn_agent"),
        Valid("from axi.config import BOT_DIR"),
        Valid("import asyncio"),
    ]
    INVALID = [
        Invalid("from axi.main import check_schedules"),
        Invalid("from axi import main"),
        Invalid("import axi.main"),
    ]
    MESSAGE = (
        "Do not import from main.py — it is the top-level orchestrator. "
        "Move the needed code to a library module instead."
    )

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        # from axi.main import ...
        if (
            isinstance(node.module, cst.Attribute)
            and node.module.attr.value == "main"
            and isinstance(node.module.value, cst.Name)
            and node.module.value.value == "axi"
        ):
            self.report(node)
        # from axi import main
        if isinstance(node.module, cst.Name) and node.module.value == "axi":
            if not isinstance(node.names, cst.ImportStar):
                for alias in node.names:
                    if isinstance(alias.name, cst.Name) and alias.name.value == "main":
                        self.report(node)

    def visit_Import(self, node: cst.Import) -> None:
        if isinstance(node.names, cst.ImportStar):
            return
        for alias in node.names:
            # import axi.main
            if (
                isinstance(alias.name, cst.Attribute)
                and alias.name.attr.value == "main"
                and isinstance(alias.name.value, cst.Name)
                and alias.name.value.value == "axi"
            ):
                self.report(node)


class NoMultiCharStrip(LintRule):
    """Flag lstrip/rstrip with multi-character string arguments.

    lstrip() and rstrip() strip a *set of characters*, not a prefix/suffix.
    `s.lstrip("* ")` strips any mix of '*' and ' ' from the left — it does NOT
    remove the prefix "* ". Use removeprefix()/removesuffix() instead.
    """

    VALID = [
        Valid("s.lstrip()"),
        Valid('s.lstrip("\\n")'),
        Valid('s.rstrip("x")'),
        Valid('s.removeprefix("* ")'),
        Valid('s.lstrip(" ")'),
    ]
    INVALID = [
        Invalid(
            's.lstrip("* ")',
            expected_message=(
                'lstrip() strips a set of characters, not a prefix. '
                'Did you mean removeprefix("* ")?'
            ),
        ),
        Invalid(
            's.rstrip("abc")',
            expected_message=(
                'rstrip() strips a set of characters, not a suffix. '
                'Did you mean removesuffix("abc")?'
            ),
        ),
    ]

    def visit_Call(self, node: cst.Call) -> None:
        # Match <expr>.lstrip(...) or <expr>.rstrip(...)
        if not isinstance(node.func, cst.Attribute):
            return
        method = node.func.attr.value
        if method not in ("lstrip", "rstrip"):
            return

        # Must have exactly one positional argument
        if len(node.args) != 1 or node.args[0].keyword is not None:
            return

        arg = node.args[0].value
        if not isinstance(arg, cst.SimpleString):
            return

        # Evaluate the string literal to get actual characters
        try:
            value = arg.evaluated_value
        except Exception:
            return

        if value is None or len(value) <= 1:
            return

        alt = "removeprefix" if method == "lstrip" else "removesuffix"
        self.report(
            node,
            f"{method}() strips a set of characters, not a "
            f'{"prefix" if method == "lstrip" else "suffix"}. '
            f'Did you mean {alt}("{value}")?',
        )


class NoBareShutilRmtree(LintRule):
    """Flag shutil.rmtree() calls.

    shutil.rmtree is a destructive operation that recursively deletes entire
    directory trees. Use a lint-fixme or lint-ignore comment for intentional use.
    """

    VALID = [
        Valid("os.remove(path)"),
        Valid("pathlib.Path(p).unlink()"),
        Valid("rmtree(path)"),  # not shutil.rmtree
    ]
    INVALID = [
        Invalid("shutil.rmtree(path)"),
        Invalid("shutil.rmtree(path, ignore_errors=True)"),
    ]
    MESSAGE = (
        "shutil.rmtree() is destructive — add a # lint-fixme comment "
        "if this is intentional."
    )

    def visit_Call(self, node: cst.Call) -> None:
        if not isinstance(node.func, cst.Attribute):
            return
        if (
            isinstance(node.func.value, cst.Name)
            and node.func.value.value == "shutil"
            and node.func.attr.value == "rmtree"
        ):
            self.report(node)


class SubprocessRequireCaptureOutput(LintRule):
    """Flag subprocess.run() without capture_output or explicit stdout/stderr.

    Uncaptured subprocess output leaks to the parent process stdout/stderr.
    Pass capture_output=True or explicit stdout=/stderr= arguments.
    """

    VALID = [
        Valid("subprocess.run(cmd, capture_output=True)"),
        Valid("subprocess.run(cmd, capture_output=True, text=True)"),
        Valid("subprocess.run(cmd, stdout=PIPE, stderr=PIPE)"),
        Valid("subprocess.run(cmd, stdout=DEVNULL, stderr=DEVNULL)"),
    ]
    INVALID = [
        Invalid("subprocess.run(cmd)"),
        Invalid("subprocess.run(cmd, check=True)"),
        Invalid("subprocess.run(cmd, text=True)"),
    ]
    MESSAGE = (
        "subprocess.run() without capture_output or explicit stdout/stderr "
        "leaks output to the parent process."
    )

    def visit_Call(self, node: cst.Call) -> None:
        if not self._is_subprocess_run(node):
            return

        kwarg_names = {
            arg.keyword.value
            for arg in node.args
            if arg.keyword is not None
        }

        if "capture_output" in kwarg_names:
            return
        if "stdout" in kwarg_names or "stderr" in kwarg_names:
            return

        self.report(node)

    @staticmethod
    def _is_subprocess_run(node: cst.Call) -> bool:
        return (
            isinstance(node.func, cst.Attribute)
            and isinstance(node.func.value, cst.Name)
            and node.func.value.value == "subprocess"
            and node.func.attr.value == "run"
        )


def _list_has_string(lst: cst.List, value: str) -> bool:
    """Check if a CST List node contains a SimpleString with the given value."""
    for el in lst.elements:
        if isinstance(el.value, cst.SimpleString):
            try:
                if el.value.evaluated_value == value:
                    return True
            except Exception:
                pass
    return False


class SystemctlRequiresEnv(LintRule):
    """Flag subprocess.run(["systemctl", "--user", ...]) without env= kwarg.

    systemctl --user silently fails in environments without XDG_RUNTIME_DIR.
    Always pass env=_systemctl_env() to ensure the user bus is reachable.
    """

    VALID = [
        Valid(
            'subprocess.run(["systemctl", "--user", "start", svc], '
            "capture_output=True, env=_systemctl_env())"
        ),
        Valid(
            'subprocess.run(["systemctl", "daemon-reload"], '
            "capture_output=True)"
        ),
        Valid("subprocess.run(cmd, capture_output=True)"),
    ]
    INVALID = [
        Invalid(
            'subprocess.run(["systemctl", "--user", "start", svc], '
            "capture_output=True)",
        ),
        Invalid(
            'subprocess.run(["systemctl", "--user", "stop", unit], '
            "capture_output=True, text=True)",
        ),
    ]
    MESSAGE = (
        "subprocess.run() calling systemctl --user must pass env=_systemctl_env() "
        "— without it, systemctl silently fails in sandboxed environments."
    )

    def visit_Call(self, node: cst.Call) -> None:
        if not self._is_subprocess_run(node):
            return

        # Find the first positional argument (the command list)
        first_arg = None
        for arg in node.args:
            if arg.keyword is None:
                first_arg = arg.value
                break

        if not isinstance(first_arg, cst.List):
            return

        # Check if it's a systemctl --user call
        if not (
            _list_has_string(first_arg, "systemctl")
            and _list_has_string(first_arg, "--user")
        ):
            return

        # Check for env= kwarg
        kwarg_names = {
            arg.keyword.value
            for arg in node.args
            if arg.keyword is not None
        }
        if "env" in kwarg_names:
            return

        self.report(node)

    @staticmethod
    def _is_subprocess_run(node: cst.Call) -> bool:
        return (
            isinstance(node.func, cst.Attribute)
            and isinstance(node.func.value, cst.Name)
            and node.func.value.value == "subprocess"
            and node.func.attr.value == "run"
        )


def _is_none_or_empty_string(node: cst.BaseExpression) -> bool:
    """Check if a CST node is None or an empty string literal."""
    if isinstance(node, cst.Name) and node.value == "None":
        return True
    if isinstance(node, cst.SimpleString):
        try:
            return node.evaluated_value == ""
        except Exception:
            return False
    return False


class NoEmptySystemPrompt(LintRule):
    """Flag system_prompt=None or system_prompt="" passed to any function.

    Passing None or "" as a system prompt silently disables it. If you don't
    want a system prompt, omit the parameter entirely. If this is a conditional
    value, ensure the caller never passes None/"".
    """

    VALID = [
        Valid('ClaudeAgentOptions(cwd="/tmp", system_prompt=prompt)'),
        Valid('ClaudeAgentOptions(cwd="/tmp", system_prompt="You are helpful.")'),
        Valid('ClaudeAgentOptions(cwd="/tmp", system_prompt=build_prompt())'),
        Valid('ClaudeAgentOptions(cwd="/tmp")'),
    ]
    INVALID = [
        Invalid('ClaudeAgentOptions(cwd="/tmp", system_prompt=None)'),
        Invalid('ClaudeAgentOptions(cwd="/tmp", system_prompt="")'),
    ]
    MESSAGE = (
        "Do not pass None or empty string as system_prompt — it silently "
        "disables the system prompt. Omit the parameter or pass a real prompt."
    )

    def visit_Call(self, node: cst.Call) -> None:
        for arg in node.args:
            if (
                arg.keyword is not None
                and arg.keyword.value == "system_prompt"
                and _is_none_or_empty_string(arg.value)
            ):
                self.report(node)
