#!/usr/bin/env bash
# Regenerate gRPC stubs from the pinned agentgateway protos. Run after bumping PIN in ../../gateway/VERSION.
set -euo pipefail
cd "$(dirname "$0")"
INC="$(python -c 'import grpc_tools,os;print(os.path.join(os.path.dirname(grpc_tools.__file__),"_proto"))')"
python -m grpc_tools.protoc -I. -I"$INC" --python_out=gen --grpc_python_out=gen --pyi_out=gen ext_proc.proto ext_mcp.proto shared_envoy.proto
echo "regenerated into gen/"
