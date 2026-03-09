"""Unit tests for axi_types.tool_display()."""

from axi.axi_types import tool_display


class TestToolDisplay:
    def test_known_tool(self) -> None:
        assert tool_display("Bash") == "running bash command"
        assert tool_display("Read") == "reading file"

    def test_mcp_prefix_parsing(self) -> None:
        assert tool_display("mcp__server__action") == "server: action"

    def test_mcp_prefix_two_parts(self) -> None:
        # Only 2 parts after splitting on __ — no server/action pair
        assert tool_display("mcp__only") == "using mcp__only"

    def test_unknown_tool(self) -> None:
        assert tool_display("SomeNewTool") == "using SomeNewTool"

    def test_empty_string(self) -> None:
        assert tool_display("") == "using "
