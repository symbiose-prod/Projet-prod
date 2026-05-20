"""
tests/test_realtime.py
======================
Tests unitaires du broker temps réel (``common/services/realtime.py``).

Pas de DB, pas de réseau — tests asyncio purs via ``asyncio.run``.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from common.services import realtime


@pytest.fixture(autouse=True)
def _reset_broker():
    """Hard reset entre chaque test pour isolation."""
    realtime._reset_for_tests()
    yield
    realtime._reset_for_tests()


# ─── broadcast / subscribe : flow nominal ──────────────────────────────────

def test_broadcast_delivers_event_to_subscriber():
    """Un broadcast doit arriver dans la queue du subscriber du même tenant."""

    async def _scenario():
        received: list[dict] = []

        async def consumer():
            async for ev in realtime.subscribe("tenant-A"):
                received.append(ev)
                return  # premier event reçu → on sort

        task = asyncio.create_task(consumer())
        # Laisse la coroutine s'enregistrer dans le set de subscribers
        await asyncio.sleep(0.02)
        realtime.broadcast("tenant-A", {"type": "palette_linked", "sscc": "X"})
        await asyncio.wait_for(task, timeout=1.0)
        return received

    received = asyncio.run(_scenario())
    assert len(received) == 1
    assert received[0]["type"] == "palette_linked"
    assert received[0]["sscc"] == "X"
    assert "ts" in received[0]  # timestamp injecté par broadcast


def test_broadcast_scoped_to_tenant():
    """Un broadcast sur tenant-A ne doit PAS atteindre un subscriber tenant-B."""

    async def _scenario():
        received_a: list[dict] = []
        received_b: list[dict] = []

        async def consumer_a():
            async for ev in realtime.subscribe("tenant-A"):
                received_a.append(ev)
                return

        async def consumer_b():
            try:
                async for ev in realtime.subscribe("tenant-B"):
                    received_b.append(ev)
                    return
            except asyncio.CancelledError:
                return

        task_a = asyncio.create_task(consumer_a())
        task_b = asyncio.create_task(consumer_b())
        await asyncio.sleep(0.02)

        realtime.broadcast("tenant-A", {"type": "test"})
        await asyncio.wait_for(task_a, timeout=1.0)

        # Donne une chance à tenant-B de recevoir (il ne doit PAS)
        await asyncio.sleep(0.05)
        task_b.cancel()
        try:
            await task_b
        except asyncio.CancelledError:
            pass
        return received_a, received_b

    received_a, received_b = asyncio.run(_scenario())
    assert len(received_a) == 1
    assert received_b == []


def test_broadcast_to_multiple_subscribers_same_tenant():
    """Tous les subscribers d'un tenant reçoivent chaque event (fan-out)."""

    async def _scenario():
        receiveds: list[list[dict]] = [[], [], []]

        async def consumer(idx: int):
            async for ev in realtime.subscribe("tenant-A"):
                receiveds[idx].append(ev)
                return

        tasks = [asyncio.create_task(consumer(i)) for i in range(3)]
        await asyncio.sleep(0.02)
        assert realtime.active_subscriber_count("tenant-A") == 3

        realtime.broadcast("tenant-A", {"type": "fanout"})
        await asyncio.gather(*tasks)
        return receiveds

    receiveds = asyncio.run(_scenario())
    assert all(len(r) == 1 and r[0]["type"] == "fanout" for r in receiveds)


def test_broadcast_no_subscribers_is_noop():
    """Broadcast sans subscriber : pas d'erreur, retour silencieux."""
    realtime.broadcast("tenant-empty", {"type": "lost"})
    assert realtime.active_subscriber_count("tenant-empty") == 0


def test_broadcast_from_worker_thread():
    """Simule un appel depuis ``asyncio.to_thread`` (link_palettes_to_ramasse).

    C'est le cas réel : les services de loading sont synchrones, exécutés
    dans un thread worker. ``broadcast()`` doit traverser correctement
    via ``call_soon_threadsafe``.
    """

    async def _scenario():
        received: list[dict] = []

        async def consumer():
            async for ev in realtime.subscribe("tenant-A"):
                received.append(ev)
                return

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.02)

        def _sync_publisher():
            realtime.broadcast("tenant-A", {"type": "from-thread"})

        await asyncio.to_thread(_sync_publisher)
        await asyncio.wait_for(task, timeout=1.0)
        return received

    received = asyncio.run(_scenario())
    assert len(received) == 1
    assert received[0]["type"] == "from-thread"


# ─── cleanup ────────────────────────────────────────────────────────────────

def test_subscriber_removed_on_exit():
    """À la sortie du ``async for``, le subscriber est retiré du bucket."""

    async def _scenario():
        async def consumer():
            # On utilise __anext__ explicite pour pouvoir aclose() ensuite —
            # garantit que le ``finally`` du generator s'exécute dans la même
            # boucle, avant qu'on vérifie le compteur.
            gen = realtime.subscribe("tenant-A")
            try:
                await gen.__anext__()
            finally:
                await gen.aclose()

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.02)
        assert realtime.active_subscriber_count("tenant-A") == 1
        realtime.broadcast("tenant-A", {"type": "bye"})
        await task
        # Après cleanup, le bucket doit être vide (et même le tenant retiré)
        assert realtime.active_subscriber_count("tenant-A") == 0
        assert realtime.active_subscriber_count() == 0

    asyncio.run(_scenario())


def test_subscribe_rejects_empty_tenant():
    """``subscribe('')`` doit lever ValueError immédiatement."""

    async def _scenario():
        with pytest.raises(ValueError):
            async for _ in realtime.subscribe(""):
                pass

    asyncio.run(_scenario())


# ─── sse_stream : format SSE conforme spec EventSource ────────────────────

def test_sse_stream_formats_events():
    """``sse_stream`` doit produire des chunks au format
    ``event: <type>\\ndata: <json>\\n\\n``.
    """

    async def _scenario():
        chunks: list[str] = []

        async def consumer():
            async for chunk in realtime.sse_stream(
                "tenant-A", heartbeat_sec=10.0,
            ):
                chunks.append(chunk)
                # On sort dès qu'on a le connected + 1 event
                if len(chunks) >= 2:
                    return

        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.02)
        realtime.broadcast("tenant-A", {"type": "palette_linked", "sscc": "Y"})
        await asyncio.wait_for(task, timeout=1.0)
        return chunks

    chunks = asyncio.run(_scenario())
    assert chunks[0] == ":connected\n\n"
    # Second chunk : event
    second = chunks[1]
    assert second.startswith("event: palette_linked\n")
    assert "\ndata: " in second
    assert second.endswith("\n\n")
    # Le payload data doit être du JSON valide avec le type
    data_line = [
        line for line in second.split("\n") if line.startswith("data: ")
    ][0]
    payload = json.loads(data_line[len("data: "):])
    assert payload["type"] == "palette_linked"
    assert payload["sscc"] == "Y"


def test_sse_stream_heartbeat_on_idle():
    """Sans event pendant ``heartbeat_sec``, un commentaire ``: ping`` est émis."""

    async def _scenario():
        chunks: list[str] = []

        async def consumer():
            async for chunk in realtime.sse_stream(
                "tenant-A", heartbeat_sec=0.05,
            ):
                chunks.append(chunk)
                if len(chunks) >= 2:
                    return

        task = asyncio.create_task(consumer())
        await asyncio.wait_for(task, timeout=1.0)
        return chunks

    chunks = asyncio.run(_scenario())
    assert chunks[0] == ":connected\n\n"
    assert chunks[1] == ": ping\n\n"
