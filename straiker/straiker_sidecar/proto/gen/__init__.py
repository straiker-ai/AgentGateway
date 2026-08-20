"""Generated gRPC stubs (protoc, pinned to agentgateway v1.4.1 protos in ../). Checked in
so the runtime image needs no grpcio-tools. Regenerate with ../regen.sh after bumping the pin.
The generated modules import each other as top-level names, hence the sys.path shim."""
import os as _os
import sys as _sys
_here = _os.path.dirname(__file__)
if _here not in _sys.path:
    _sys.path.insert(0, _here)
import ext_proc_pb2, ext_proc_pb2_grpc, ext_mcp_pb2, ext_mcp_pb2_grpc, shared_envoy_pb2  # noqa: E402,F401
