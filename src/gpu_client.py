"""
gRPC GPU Client — Phase 1: submit/get/stream/cancel job support.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

import grpc
from grpc import aio

# 2026-07-24: ported from github.com/casper-bot-wodinga/gpu-compute — that
# repo has `generated/` as a sibling of `orchestrator/` at its root, so the
# bare `from generated import ...` below resolved via its own cwd/layout.
# Here `generated/` lives at this repo's root instead (kept as a top-level
# package rather than nested under src/, to avoid rewriting the generated
# protobuf stubs) — make sure it's importable regardless of caller cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from generated import gpu_compute_pb2 as pb
from generated import gpu_compute_pb2_grpc as pb_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [gpu_client] %(levelname)s %(message)s",
)
logger = logging.getLogger("gpu_client")

DEFAULT_ADDRESS = os.getenv("GPU_WORKER", "legend-of-macs.local:5002")


def _resolve_hostname(address: str) -> str:
    """gRPC's own channel resolver doesn't reliably handle local-network
    hostnames — mDNS `.local` names confirmed failing 2026-07-24
    (`legend-of-macs.local:5002` errors inside grpc's resolver while the
    system resolver handles it fine), and custom local-DNS suffixes like
    `.klo` (the Mac's hostname has been both `legend-of-macs.local` and
    `imac.klo` at different times) aren't guaranteed to fare any better.
    Resolve via the system resolver (same one curl/socket use) up front for
    any non-numeric host, rather than hardcoding an IP — this network's IPs
    drift over time, that's the whole reason to use a hostname at all."""
    host, _, port = address.rpartition(":")
    if not host or host.replace(".", "").isdigit():
        return address  # already a bare IP (or malformed — let grpc raise)
    import socket
    try:
        ip = socket.getaddrinfo(host, int(port))[0][4][0]
        return f"{ip}:{port}"
    except socket.gaierror as e:
        logger.warning("Hostname resolution failed for %s, using as-is: %s", address, e)
        return address


class GpuClient:
    """Async wrapper around a GpuWorker gRPC channel."""

    stub: pb_grpc.GpuWorkerStub
    _channel: grpc.aio.Channel

    def __init__(self, address: str = DEFAULT_ADDRESS) -> None:
        self._address = _resolve_hostname(address)
        self._channel = grpc.aio.insecure_channel(self._address)
        self.stub = pb_grpc.GpuWorkerStub(self._channel)

    async def close(self) -> None:
        await self._channel.close()

    # -- Phase 2 RPCs (file transfer) --

    async def upload_file(
        self,
        local_path: str,
        job_id: str = "",
        staging_subdir: str = "input",
        chunk_size: int = 64 * 1024,
    ) -> Optional[pb.UploadResponse]:
        """Upload a local file to the worker via streaming FileChunks.

        Args:
            local_path: Path to the file to upload
            job_id: Optional job id for scoped staging
            staging_subdir: Subdirectory under job dir (e.g. "input", "output")
            chunk_size: Bytes per chunk (default 64KB)

        Returns:
            UploadResponse with ok, stored_path, sha256, or None on error.
        """
        import hashlib

        file_path = Path(local_path)
        if not file_path.exists():
            logger.error("upload_file: file not found: %s", local_path)
            return None

        file_size = file_path.stat().st_size
        # Compute sha256
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                block = f.read(chunk_size)
                if not block:
                    break
                hasher.update(block)
        sha256_hex = hasher.hexdigest()

        async def chunk_generator():
            # Meta chunk first
            yield pb.FileChunk(
                meta=pb.FileMeta(
                    filename=file_path.name,
                    size=file_size,
                    sha256=sha256_hex,
                    job_id=job_id,
                    staging_subdir=staging_subdir,
                )
            )
            # Data chunks
            with open(file_path, "rb") as f:
                while True:
                    data = f.read(chunk_size)
                    if not data:
                        break
                    yield pb.FileChunk(data=data)

        try:
            response = await self.stub.UploadFile(chunk_generator(), timeout=120)
            logger.info(
                "upload_file: %s \u2192 %s (sha256=%s)",
                local_path, response.stored_path, response.sha256,
            )
            return response
        except grpc.aio.AioRpcError as e:
            logger.error("UploadFile failed: %s (%s)", e.code(), e.details())
            return None

    async def download_file(
        self,
        remote_path: str,
        local_path: str,
        job_id: str = "",
        staging_subdir: str = "",
    ) -> bool:
        """Download a file from the worker via streaming FileChunks.

        Args:
            remote_path: Path on the worker (or filename if job_id provided)
            local_path: Where to save the file locally
            job_id: Optional job id for scoped lookup
            staging_subdir: Optional staging subdirectory (e.g. "input", "output")

        Returns:
            True if download succeeded and sha256 verified, False otherwise.
        """
        import hashlib

        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)

        request = pb.FileRequest(path=remote_path, job_id=job_id, staging_subdir=staging_subdir)
        hasher = hashlib.sha256()
        expected_sha = ""
        total_bytes = 0

        try:
            async for chunk in self.stub.DownloadFile(request, timeout=120):
                if chunk.WhichOneof("kind") == "meta":
                    expected_sha = chunk.meta.sha256
                    logger.info(
                        "download_file: receiving %s (%d bytes, sha256=%s)",
                        chunk.meta.filename, chunk.meta.size, expected_sha,
                    )
                    # Truncate/create file
                    open(local, "wb").close()
                elif chunk.WhichOneof("kind") == "data":
                    with open(local, "ab") as f:
                        f.write(chunk.data)
                    hasher.update(chunk.data)
                    total_bytes += len(chunk.data)

        except grpc.aio.AioRpcError as e:
            logger.error("DownloadFile failed: %s (%s)", e.code(), e.details())
            if local.exists():
                local.unlink()
            return False

        actual_sha = hasher.hexdigest()
        if expected_sha and actual_sha != expected_sha:
            logger.error(
                "download_file: SHA256 mismatch \u2014 expected %s, got %s",
                expected_sha, actual_sha,
            )
            if local.exists():
                local.unlink()
            return False

        logger.info(
            "download_file: %s \u2190 %s (%d bytes, sha256=%s)",
            local_path, remote_path, total_bytes, actual_sha,
        )
        return True

    async def health(self) -> Optional[pb.HealthResponse]:
        try:
            return await self.stub.Health(pb.HealthRequest(), timeout=5)
        except grpc.aio.AioRpcError as e:
            logger.error("Health RPC failed: %s (%s)", e.code(), e.details())
            return None

    async def capabilities(self) -> Optional[pb.CapabilitiesResponse]:
        try:
            return await self.stub.GetCapabilities(pb.CapabilitiesRequest(), timeout=5)
        except grpc.aio.AioRpcError as e:
            logger.error("GetCapabilities RPC failed: %s (%s)", e.code(), e.details())
            return None

    # -- Phase 1 RPCs --

    async def submit_embed(
        self,
        staged_index_path: str,
        collection: str,
        model: str = "all-MiniLM-L6-v2",
        idempotency_key: str = "",
    ) -> Optional[str]:
        """Submit an embedding job. Returns job_id or None on error."""
        req = pb.JobRequest(
            job_type="embed",
            embed=pb.EmbedJob(
                staged_index_path=staged_index_path,
                collection=collection,
                args={"model": model},
            ),
            idempotency_key=idempotency_key,
        )
        try:
            handle = await self.stub.SubmitJob(req, timeout=10)
            logger.info("Submitted embed job: %s", handle.job_id)
            return handle.job_id
        except grpc.aio.AioRpcError as e:
            logger.error("SubmitJob (embed) failed: %s (%s)", e.code(), e.details())
            return None

    async def submit_train(
        self,
        model_type: str,
        symbol: str,
        data_path: str,
        n_components: int = 4,
        n_iter: int = 100,
        write_reload_flag: bool = True,
        idempotency_key: str = "",
    ) -> Optional[str]:
        """Submit an HMM training job. Returns job_id or None on error."""
        req = pb.JobRequest(
            job_type="train",
            train=pb.TrainJob(
                model_type=model_type,
                symbol=symbol,
                params={
                    "n_components": str(n_components),
                    "n_iter": str(n_iter),
                    "data_path": data_path,
                },
                write_reload_flag=write_reload_flag,
            ),
            idempotency_key=idempotency_key,
        )
        try:
            handle = await self.stub.SubmitJob(req, timeout=10)
            logger.info("Submitted train job: %s (symbol=%s)", handle.job_id, symbol)
            return handle.job_id
        except grpc.aio.AioRpcError as e:
            logger.error("SubmitJob (train) failed: %s (%s)", e.code(), e.details())
            return None

    async def submit_infer(
        self,
        model_name: str,
        features: list,
        idempotency_key: str = "",
    ) -> Optional[str]:
        """Submit an inference job. features is a list of feature rows. Returns job_id or None."""
        req = pb.JobRequest(
            job_type="infer",
            infer=pb.InferenceJob(
                model_name=model_name,
                features_json=json.dumps(features).encode(),
            ),
            idempotency_key=idempotency_key,
        )
        try:
            handle = await self.stub.SubmitJob(req, timeout=10)
            logger.info("Submitted infer job: %s (model=%s)", handle.job_id, model_name)
            return handle.job_id
        except grpc.aio.AioRpcError as e:
            logger.error("SubmitJob (infer) failed: %s (%s)", e.code(), e.details())
            return None

    async def get_job(self, job_id: str) -> Optional[pb.JobUpdate]:
        """Poll job status once."""
        try:
            return await self.stub.GetJob(pb.JobStatusRequest(job_id=job_id), timeout=5)
        except grpc.aio.AioRpcError as e:
            logger.error("GetJob failed: %s (%s)", e.code(), e.details())
            return None

    async def stream_job(self, job_id: str) -> AsyncIterator[pb.JobUpdate]:
        """Stream job updates until terminal state."""
        try:
            async for update in self.stub.StreamJob(pb.JobStatusRequest(job_id=job_id)):
                yield update
        except grpc.aio.AioRpcError as e:
            logger.error("StreamJob failed: %s (%s)", e.code(), e.details())

    async def cancel_job(self, job_id: str) -> Optional[pb.JobUpdate]:
        try:
            return await self.stub.CancelJob(pb.JobStatusRequest(job_id=job_id), timeout=5)
        except grpc.aio.AioRpcError as e:
            logger.error("CancelJob failed: %s (%s)", e.code(), e.details())
            return None

    async def wait_for_job(self, job_id: str, timeout: float = 300.0) -> Optional[pb.JobUpdate]:
        """Poll GetJob until terminal, with timeout. Returns final JobUpdate."""
        deadline = asyncio.get_event_loop().time() + timeout
        terminal = {pb.JobPhase.COMPLETED, pb.JobPhase.FAILED, pb.JobPhase.CANCELLED}
        while asyncio.get_event_loop().time() < deadline:
            update = await self.get_job(job_id)
            if update is None:
                return None
            if update.phase in terminal:
                return update
            await asyncio.sleep(1.0)
        logger.warning("wait_for_job timed out after %.0fs: %s", timeout, job_id)
        return None


class WorkerPool:
    """
    Routes GPU jobs across a pool of GpuClient workers.

    Workers are selected by lowest queue_depth among healthy nodes.
    Maintains a job\u2192worker registry so wait_for_job() always hits
    the right worker regardless of pool size.

    Configuration (checked in order):
        GPU_WORKERS=addr1,addr2,...   comma-separated list (takes precedence)
        GPU_WORKER=addr               single worker (legacy)
        default: 192.168.1.237:5002
    """

    DEFAULT_ADDRESS = os.getenv("GPU_WORKER", "legend-of-macs.local:5002")

    def __init__(self, addresses: list[str]) -> None:
        if not addresses:
            raise ValueError("WorkerPool requires at least one address")
        self._clients = [GpuClient(addr) for addr in addresses]
        self._job_registry: dict[str, GpuClient] = {}  # job_id \u2192 client

    @classmethod
    def from_env(cls) -> "WorkerPool":
        """Build pool from GPU_WORKERS or GPU_WORKER env vars."""
        import os
        multi = os.getenv("GPU_WORKERS", "")
        if multi:
            addresses = [a.strip() for a in multi.split(",") if a.strip()]
        else:
            addresses = [os.getenv("GPU_WORKER", cls.DEFAULT_ADDRESS)]
        logger.info("WorkerPool: %d worker(s): %s", len(addresses), addresses)
        return cls(addresses)

    async def pick(self) -> Optional["GpuClient"]:
        """Return the least-loaded healthy worker, or None if all are down."""
        checks = await asyncio.gather(*[c.health() for c in self._clients])
        candidates = [
            (c, h) for c, h in zip(self._clients, checks)
            if h is not None
        ]
        if not candidates:
            logger.error("WorkerPool: no healthy workers available")
            return None
        # Prefer idle > busy > any; break ties by queue_depth
        candidates.sort(key=lambda ch: ch[1].queue_depth)
        chosen, h = candidates[0]
        logger.info(
            "WorkerPool: picked %s (queue_depth=%d, state=%s)",
            chosen._address, h.queue_depth, pb.WorkerState.Name(h.state),
        )
        return chosen

    def _register(self, job_id: str, client: "GpuClient") -> None:
        self._job_registry[job_id] = client

    async def upload_file(self, *args, **kwargs) -> Optional[pb.UploadResponse]:
        """Upload a file to the least-loaded worker. Returns (UploadResponse, client) \u2014 use
        the same client for any subsequent submit on this job to keep state on one worker."""
        client = await self.pick()
        if client is None:
            return None
        return await client.upload_file(*args, **kwargs)

    async def download_file(self, *args, **kwargs) -> bool:
        """Download from any worker (caller must know remote_path is accessible)."""
        client = await self.pick()
        if client is None:
            return False
        return await client.download_file(*args, **kwargs)

    async def submit_embed(self, **kwargs) -> Optional[str]:
        client = await self.pick()
        if client is None:
            return None
        job_id = await client.submit_embed(**kwargs)
        if job_id:
            self._register(job_id, client)
        return job_id

    async def submit_train(self, **kwargs) -> Optional[str]:
        client = await self.pick()
        if client is None:
            return None
        job_id = await client.submit_train(**kwargs)
        if job_id:
            self._register(job_id, client)
        return job_id

    async def submit_infer(self, **kwargs) -> Optional[str]:
        client = await self.pick()
        if client is None:
            return None
        job_id = await client.submit_infer(**kwargs)
        if job_id:
            self._register(job_id, client)
        return job_id

    async def wait_for_job(self, job_id: str, timeout: float = 300.0) -> Optional[pb.JobUpdate]:
        client = self._job_registry.get(job_id)
        if client is None:
            logger.error("WorkerPool: no registered worker for job %s", job_id)
            return None
        result = await client.wait_for_job(job_id, timeout=timeout)
        if result is not None:
            self._job_registry.pop(job_id, None)
        return result

    async def close(self) -> None:
        await asyncio.gather(*[c.close() for c in self._clients])


async def smoke_test(address: str = DEFAULT_ADDRESS) -> int:
    client = GpuClient(address)

    h = await client.health()
    if h is None:
        logger.warning("Worker not reachable (expected if not deployed yet)")
        await client.close()
        return 1

    logger.info(
        "Health: worker_id=%s state=%s queue_depth=%d gpu_mem_free_mb=%.1f uptime_sec=%d",
        h.worker_id, pb.WorkerState.Name(h.state), h.queue_depth, h.gpu_mem_free_mb, h.uptime_sec,
    )

    c = await client.capabilities()
    if c is not None:
        logger.info(
            "Capabilities: worker_id=%s device=%s jobs=%s models=%s",
            c.worker_id, c.device, list(c.job_types), list(c.models),
        )

    await client.close()
    return 0


if __name__ == "__main__":
    address = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ADDRESS
    sys.exit(asyncio.run(smoke_test(address)))