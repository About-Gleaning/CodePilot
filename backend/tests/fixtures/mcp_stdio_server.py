from mcp.server.fastmcp import FastMCP


server = FastMCP("CodePilot MCP Test")


@server.tool()
def echo(text: str) -> str:
    """返回输入文本，用于验证真实 stdio MCP 调用链。"""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
