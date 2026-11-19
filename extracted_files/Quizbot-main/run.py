#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from quizbot.database import init_db, close_db
from quizbot.shared import config
from quizbot.shared.utils.http import close_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("launcher")

for noisy in ("httpx", "httpcore", "apscheduler", "pymongo"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


async def run_creator():
    from quizbot.creator_bot.bot import run_creator_bot
    logger.info("Starting Creator Bot...")
    await run_creator_bot()


async def run_runner():
    from quizbot.runner_bot.bot import run_runner_bot
    logger.info("Starting Player Bot...")
    await run_runner_bot()


async def run_mini_app():
    from quizbot.mini_app.server import run_mini_app_server
    logger.info("Starting Mini App...")
    await run_mini_app_server()


async def health_handler(reader, writer):
    try:
        request = await asyncio.wait_for(reader.readline(), timeout=5)
        path = request.decode(errors="ignore").split(" ")[1]

        # Read and discard headers
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break

        if path in ("/", "/healthz"):
            body = b"PSCGram Quiz Bot is running"
            status = b"200 OK"
        else:
            body = b"Not Found"
            status = b"404 Not Found"

        response = (
            b"HTTP/1.1 " + status + b"\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        await writer.drain()

    except Exception:
        logger.warning("Health-check request failed")

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def run_health_server(stop_event):
    port = int(os.environ.get("PORT", "10000"))

    server = await asyncio.start_server(
        health_handler,
        host="0.0.0.0",
        port=port,
    )

    logger.info("Health server listening on port %s", port)

    async with server:
        await stop_event.wait()


async def main(only=None):
    problems = config.validate(bot=only or "both")

    if problems:
        for problem in problems:
            logger.error("Config problem: %s", problem)
        sys.exit(1)

    logger.info("Connecting to MongoDB...")
    await init_db(config.MONGODB_URI, config.MONGODB_DB_NAME)
    logger.info("Database connected.")

    stop_event = asyncio.Event()
    tasks = []

    if only in (None, "creator"):
        tasks.append(asyncio.create_task(run_creator(), name="creator"))

    if only in (None, "runner"):
        tasks.append(asyncio.create_task(run_runner(), name="runner"))

    if only == "miniapp":
        tasks.append(asyncio.create_task(run_mini_app(), name="miniapp"))

    elif only is None and config.MINI_APP_DOMAIN:
        tasks.append(asyncio.create_task(run_mini_app(), name="miniapp"))

    # Start HTTP health server for Render Web Service
    if only is None:
        tasks.append(
            asyncio.create_task(
                run_health_server(stop_event),
                name="health_server",
            )
        )

    def handle_signal(*_):
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_signal)
        except NotImplementedError:
            pass

    try:
        stop_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait(
            tasks + [stop_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in done:
            if task is not stop_task and not task.cancelled():
                error = task.exception()
                if error:
                    logger.error(
                        "Task %s crashed: %s",
                        task.get_name(),
                        error,
                    )

    finally:
        logger.info("Shutting down...")

        stop_event.set()

        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        await close_session()
        await close_db()

        logger.info("Shutdown complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        choices=["creator", "runner", "miniapp"],
        default=None,
    )
    args = parser.parse_args()

    try:
        asyncio.run(main(args.only))
    except KeyboardInterrupt:
        pass
