"""
Cloudflare Quick Tunnel — 대시보드를 외부 인터넷에 공개.

cloudflared.exe를 서브프로세스로 실행해 localhost:8000을
외부에서 접속 가능한 HTTPS URL로 노출합니다.

- 계정 불필요 (Quick Tunnel)
- 매 실행마다 새 URL 발급 (trycloudflare.com 도메인)
- URL 확인 즉시 텔레그램으로 알림 발송
"""

import asyncio
import logging
import re
from pathlib import Path
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# cloudflared 출력에서 URL을 찾는 패턴
_URL_PATTERN = re.compile(r"https://[a-z0-9\-]+\.trycloudflare\.com")


class CloudflareTunnel:
    """cloudflared Quick Tunnel을 비동기로 실행합니다."""

    def __init__(
        self,
        cloudflared_path: str,
        local_port: int = 8000,
        on_url_ready: Optional[Callable[[str], Coroutine]] = None,
    ) -> None:
        self._exe = str(Path(cloudflared_path).resolve())
        self._port = local_port
        self._on_url_ready = on_url_ready
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._url: Optional[str] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def url(self) -> Optional[str]:
        return self._url

    def start(self) -> None:
        """비동기 태스크로 터널 시작."""
        self._task = asyncio.create_task(self._run(), name="cloudflare_tunnel")

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except Exception:
                pass
        if self._task:
            self._task.cancel()
        logger.info("[Tunnel] Stopped.")

    async def _run(self) -> None:
        cmd = [
            self._exe,
            "tunnel",
            "--url", f"http://localhost:{self._port}",
            "--no-autoupdate",
        ]
        logger.info("[Tunnel] Starting cloudflared → http://localhost:%d", self._port)

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError:
            logger.error("[Tunnel] cloudflared not found at: %s", self._exe)
            return
        except Exception as exc:
            logger.error("[Tunnel] Failed to start cloudflared: %s", exc)
            return

        async for raw_line in self._proc.stdout:
            text = raw_line.decode("utf-8", errors="replace").strip()
            logger.debug("[Tunnel] %s", text)

            # URL이 아직 없을 때 출력에서 URL 파싱
            if self._url is None:
                match = _URL_PATTERN.search(text)
                if match:
                    self._url = match.group(0)
                    logger.info("[Tunnel] Public URL ready: %s", self._url)
                    if self._on_url_ready:
                        try:
                            await self._on_url_ready(self._url)
                        except Exception as exc:
                            logger.warning("[Tunnel] on_url_ready callback error: %s", exc)

        await self._proc.wait()
        logger.warning("[Tunnel] cloudflared exited (code=%s)", self._proc.returncode)
