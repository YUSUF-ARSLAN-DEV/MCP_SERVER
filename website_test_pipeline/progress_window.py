"""A small always-on-top window: timer, progress bar and a rough time-left estimate for a running pipeline.

    python -m website_test_pipeline.progress_window [path/to/progress.json]

`all` opens it for you (skip with --no-window). Hide button minimises it to the taskbar; the pin button
toggles always-on-top. It only reads progress.json, so closing it never affects the run.
"""
from __future__ import annotations
import json
import sys
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk


def fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(path: Path) -> None:
    root = tk.Tk()
    root.title("Pipeline progress")
    root.geometry("360x150")
    root.resizable(False, False)
    root.attributes("-topmost", True)
    pinned = tk.BooleanVar(value=True)

    phase = tk.StringVar(value="Waiting for the run to start...")
    note = tk.StringVar()
    clock = tk.StringVar(value="00:00")
    eta = tk.StringVar()
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, textvariable=phase, font=("Segoe UI", 10, "bold")).pack(anchor="w")
    ttk.Label(frame, textvariable=note, foreground="gray").pack(anchor="w")
    bar = ttk.Progressbar(frame, maximum=100, length=330)
    bar.pack(pady=6)
    row = ttk.Frame(frame)
    row.pack(fill="x")
    ttk.Label(row, textvariable=clock, font=("Consolas", 14)).pack(side="left")
    ttk.Label(row, textvariable=eta).pack(side="left", padx=10)
    ttk.Button(row, text="Hide", width=6, command=root.iconify).pack(side="right")
    ttk.Checkbutton(row, text="Pin", variable=pinned, command=lambda: root.attributes("-topmost", pinned.get())).pack(side="right", padx=4)

    state: dict = {}

    def poll() -> None:
        data = read(path)
        if data:
            state.update(data)
            phase.set(data["phase"] + (" - finished" if data["finished"] and data["phase"] != "Done" else ""))
            note.set(f"{data['done']}/{data['total']}  {data['note']}"[:60] if data["total"] else data["note"][:60])
            bar["value"] = data["fraction"] * 100
        root.after(1000, poll)

    def tick() -> None:
        if state:
            end = state["updated"] if state["finished"] else time.time()
            elapsed = end - state["started"]
            clock.set(fmt(elapsed))
            frac = state["fraction"]
            if state["finished"]:
                eta.set("done")
            elif frac >= 0.03:
                eta.set(f"~{fmt(elapsed / frac * (1 - frac))} left (rough)")
            else:
                eta.set("estimating...")
        root.after(500, tick)

    poll()
    tick()
    root.mainloop()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("progress.json"))
