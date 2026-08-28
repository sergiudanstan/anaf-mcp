"""Entry point Vercel pentru endpointul public /mcp."""

from remote_mcp import RemoteMCPHandler


class handler(RemoteMCPHandler):
    """Clasa explicita este detectata de runtime-ul Python Vercel."""

    pass
