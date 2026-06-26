#!/usr/bin/env python3
"""Build an executive PPTX for the AI SSD HiCache pre-study.

The environment intentionally avoids python-pptx.  We generate charts with
matplotlib and write a small standards-compliant PPTX package directly.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib.pyplot as plt
from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
PLOTS = RESULTS / "plots"
ASSETS = RESULTS / "boss_ppt_assets"
REPORTS = ROOT / "reports"
OUT = REPORTS / "ai-ssd-boss-review-2026-06-26.pptx"

ASSETS.mkdir(exist_ok=True)
REPORTS.mkdir(exist_ok=True)

plt.rcParams["font.sans-serif"] = [
    "Noto Sans CJK SC",
    "Noto Sans CJK HK",
    "DejaVu Sans",
    "sans-serif",
]
plt.rcParams["axes.unicode_minus"] = False

COLORS = {
    "bg": "#07111f",
    "panel": "#0e1b2d",
    "grid": "#27415f",
    "text": "#edf6ff",
    "muted": "#9fb5c8",
    "accent": "#00d4ff",
    "accent2": "#7cffb2",
    "warn": "#ffcc66",
    "danger": "#ff6b7a",
    "BIWIN": "#00d4ff",
    "ZHITAI": "#7cffb2",
    "WDC": "#ffcc66",
    "Seagate": "#ff6b7a",
}

DISKS = ["BIWIN", "ZHITAI", "WDC", "Seagate"]
DISPLAY = {
    "BIWIN": "BIWIN\next4",
    "ZHITAI": "ZHITAI\nNTFS",
    "WDC": "WDC\nNTFS",
    "Seagate": "Seagate\nNTFS",
}


def _style_ax(ax, title: str | None = None):
    ax.set_facecolor(COLORS["bg"])
    ax.figure.set_facecolor(COLORS["bg"])
    ax.tick_params(colors=COLORS["muted"], labelsize=10)
    for spine in ax.spines.values():
        spine.set_color(COLORS["grid"])
    ax.grid(True, color=COLORS["grid"], alpha=0.35, linewidth=0.8)
    if title:
        ax.set_title(title, color=COLORS["text"], fontsize=16, weight="bold", pad=14)


def save_fig(fig, name: str) -> Path:
    path = ASSETS / name
    fig.savefig(path, dpi=180, bbox_inches="tight", facecolor=COLORS["bg"])
    plt.close(fig)
    return path


def load_latency_summary():
    rows = json.loads((RESULTS / "multiprompt_g_summary.json").read_text())
    by_disk = {r["disk"]: r for r in rows}
    return by_disk


def load_io_summary():
    rows = list(csv.DictReader((RESULTS / "io_pattern_analysis.csv").open()))
    by_disk = {}
    for disk in ["BIWIN", "WDC", "Seagate", "ZHITAI"]:
        dr = [r for r in rows if r["disk"] == disk]
        def mean(k):
            return statistics.mean(float(r[k]) for r in dr)
        by_disk[disk] = {
            "read_peak": mean("read_mb_peak"),
            "read_active": mean("read_mb_mean_active"),
            "total_read": mean("total_read_mb"),
            "total_write": mean("total_write_mb"),
            "r_await": mean("r_await_mean_active"),
            "r_await_p99": mean("r_await_p99_active"),
            "req_kb": mean("rareq_sz_mean_active"),
            "util": mean("pct_util_mean_active"),
            "bursts": mean("n_bursts"),
        }
    return by_disk


def chart_exec_ranking(lat):
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    disks = DISKS
    means = [lat[d]["mean"] for d in disks]
    errs = [lat[d]["stdev"] for d in disks]
    bars = ax.bar(
        range(len(disks)),
        means,
        yerr=errs,
        capsize=7,
        color=[COLORS[d] for d in disks],
        edgecolor="#dff7ff",
        linewidth=1.2,
    )
    _style_ax(ax, "Phase7 G: L3 reload latency, 6-run mean")
    ax.set_ylabel("Replay p0 latency (s)", color=COLORS["muted"], fontsize=12)
    ax.set_xticks(range(len(disks)))
    ax.set_xticklabels([DISPLAY[d] for d in disks], color=COLORS["text"], fontsize=12)
    ax.set_ylim(0, 3.8)
    for b, d, m, e in zip(bars, disks, means, errs):
        ax.text(
            b.get_x() + b.get_width() / 2,
            m + e + 0.12,
            f"{m:.2f}s\nCV {lat[d]['cv_pct']:.1f}%",
            color=COLORS["text"],
            ha="center",
            va="bottom",
            fontsize=12,
            weight="bold",
        )
    ax.text(
        0.02,
        0.96,
        "Decision: BIWIN path fastest; among NTFS data disks, ZHITAI wins, Seagate has tail risk.",
        transform=ax.transAxes,
        color=COLORS["accent2"],
        fontsize=12,
        va="top",
    )
    return save_fig(fig, "boss_01_ranking.png")


def chart_single_vs_multi(lat):
    v3 = {
        "BIWIN": 1.663,
        "Seagate": 2.431,
        "ZHITAI": 2.545,
        "WDC": 2.643,
    }
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    x = list(range(len(DISKS)))
    w = 0.36
    ax.bar(
        [i - w / 2 for i in x],
        [v3[d] for d in DISKS],
        width=w,
        label="v3 single run",
        color="#2a6f97",
    )
    ax.bar(
        [i + w / 2 for i in x],
        [lat[d]["mean"] for d in DISKS],
        width=w,
        label="6-run mean",
        color=[COLORS[d] for d in DISKS],
    )
    _style_ax(ax, "Single-run ranking is not enough")
    ax.set_ylabel("Replay p0 latency (s)", color=COLORS["muted"], fontsize=12)
    ax.set_xticks(x)
    ax.set_xticklabels([DISPLAY[d] for d in DISKS], color=COLORS["text"], fontsize=12)
    ax.legend(facecolor=COLORS["panel"], edgecolor=COLORS["grid"], labelcolor=COLORS["text"])
    ax.set_ylim(0, 3.5)
    for i, d in enumerate(DISKS):
        ax.text(i + w / 2, lat[d]["mean"] + 0.08, f"{lat[d]['mean']:.2f}", ha="center", color=COLORS["text"], fontsize=11)
    ax.annotate(
        "Seagate looked good once,\nbut is worst on mean",
        xy=(3.15, lat["Seagate"]["mean"]),
        xytext=(2.3, 3.25),
        color=COLORS["danger"],
        fontsize=13,
        arrowprops=dict(arrowstyle="->", color=COLORS["danger"], lw=2),
    )
    return save_fig(fig, "boss_02_single_vs_multi.png")


def chart_io_latency(io):
    fig, ax1 = plt.subplots(figsize=(11.5, 6.2))
    x = list(range(len(DISKS)))
    req = [io[d]["req_kb"] for d in DISKS]
    await_ms = [io[d]["r_await"] for d in DISKS]
    bars = ax1.bar(x, req, color=[COLORS[d] for d in DISKS], alpha=0.88)
    _style_ax(ax1, "HiCache replay IO shape: small reads, latency-bound")
    ax1.set_ylabel("Average read request size (KB)", color=COLORS["muted"], fontsize=12)
    ax1.set_xticks(x)
    ax1.set_xticklabels([DISPLAY[d] for d in DISKS], color=COLORS["text"], fontsize=12)
    ax1.set_ylim(0, 140)
    ax2 = ax1.twinx()
    ax2.plot(x, await_ms, color="#ffffff", marker="o", linewidth=3, markersize=8, label="r_await mean")
    ax2.set_ylabel("r_await mean (ms)", color=COLORS["text"], fontsize=12)
    ax2.tick_params(colors=COLORS["text"])
    ax2.set_ylim(0, 0.8)
    await_label_offsets = {
        "BIWIN": (0.00, 0.05),
        "ZHITAI": (0.00, 0.05),
        "WDC": (-0.12, 0.09),
        "Seagate": (0.00, 0.04),
    }
    for i, d in enumerate(DISKS):
        ax1.text(i, req[i] + 4, f"{req[i]:.0f}KB", ha="center", color=COLORS["text"], fontsize=11)
        dx, dy = await_label_offsets[d]
        ax2.text(i + dx, await_ms[i] + dy, f"{await_ms[i]:.2f}ms", ha="center", color="#ffffff", fontsize=11, weight="bold")
    ax1.text(
        0.02,
        0.95,
        "Not a 1MB sequential fio workload: replay breaks into ~60-125KB reads.",
        transform=ax1.transAxes,
        color=COLORS["accent2"],
        fontsize=12,
        va="top",
    )
    return save_fig(fig, "boss_03_io_shape.png")


def chart_util_vs_peak(io):
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    x = [io[d]["util"] for d in DISKS]
    y = [io[d]["read_peak"] for d in DISKS]
    sizes = [max(120, io[d]["r_await_p99"] * 140) for d in DISKS]
    _style_ax(ax, "Disks are not saturated: tail latency matters more than peak BW")
    for d, xx, yy, ss in zip(DISKS, x, y, sizes):
        ax.scatter(xx, yy, s=ss, color=COLORS[d], edgecolor="white", linewidth=1.5, alpha=0.9)
        ax.text(xx + 0.8, yy + 20, d, color=COLORS["text"], fontsize=12, weight="bold")
    ax.set_xlabel("Active %util mean", color=COLORS["muted"], fontsize=12)
    ax.set_ylabel("Read peak during replay (MB/s)", color=COLORS["muted"], fontsize=12)
    ax.set_xlim(0, 50)
    ax.set_ylim(0, 1300)
    ax.text(
        0.02,
        0.94,
        "Bubble size = r_await p99. Seagate/WDC tail is visible even without 100% util.",
        transform=ax.transAxes,
        color=COLORS["accent2"],
        fontsize=12,
        va="top",
    )
    return save_fig(fig, "boss_04_util_tail.png")


def chart_evidence_funnel():
    stages = [
        ("Phase0\nLMCache", "Proves KV reuse\n23x TTFT win", 0.15),
        ("Phase2/4/5\nCold-Warm", "Shows L2 hides SSD\n<30ms spread", 0.38),
        ("Phase6\nfio", "Hardware upper bound\n2.6-4.8 GB/s", 0.62),
        ("Phase7\nReplay", "Forces L2 miss\n0.98s spread", 0.82),
        ("Phase7 G\n6 runs", "Selection basis\nmean + tail", 1.0),
    ]
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    ax.set_facecolor(COLORS["bg"])
    fig.set_facecolor(COLORS["bg"])
    ax.axis("off")
    y = 0.82
    for i, (title, body, width) in enumerate(stages):
        x0 = 0.5 - width / 2
        rect = plt.Rectangle((x0, y - i * 0.16), width, 0.105, color=COLORS["panel"], ec=COLORS["accent"], lw=1.6)
        ax.add_patch(rect)
        ax.text(0.5, y + 0.052 - i * 0.16, title, ha="center", va="center", color=COLORS["text"], fontsize=13, weight="bold")
        ax.text(0.5, y + 0.01 - i * 0.16, body, ha="center", va="center", color=COLORS["muted"], fontsize=11)
        if i < len(stages) - 1:
            ax.annotate("", xy=(0.5, y - 0.055 - i * 0.16), xytext=(0.5, y - 0.005 - i * 0.16),
                        arrowprops=dict(arrowstyle="->", color=COLORS["accent2"], lw=2))
    ax.text(0.5, 0.96, "Test design: from cache validation to selection-grade evidence", ha="center", color=COLORS["text"], fontsize=18, weight="bold")
    return save_fig(fig, "boss_05_evidence_funnel.png")


def chart_decision_matrix(lat, io):
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    ax.axis("off")
    fig.set_facecolor(COLORS["bg"])
    ax.set_facecolor(COLORS["bg"])
    headers = ["Drive", "Role", "Latency", "Stability", "IO diagnosis", "Decision"]
    rows = [
        ["BIWIN", "system ext4", "1.62s", "CV 1.3%", "page-cache path", "Use for lowest latency"],
        ["ZHITAI", "NTFS data", "2.27s", "CV 7.7%", "best await/tail", "Best data SSD"],
        ["WDC", "NTFS data", "2.65s", "CV 6.0%", "stable mid", "Capacity fallback"],
        ["Seagate", "NTFS data", "2.98s", "CV 18.1%", "bimodal slow read", "Avoid hot L3 alone"],
    ]
    colw = [0.13, 0.17, 0.12, 0.13, 0.22, 0.23]
    x0, y0, rowh = 0.03, 0.78, 0.12
    ax.text(0.03, 0.94, "Decision matrix", color=COLORS["text"], fontsize=20, weight="bold", ha="left")
    ax.text(0.03, 0.89, "Recommendation uses 6-run latency plus IO diagnosis, not single-run peak.", color=COLORS["muted"], fontsize=12, ha="left")
    x = x0
    for h, w in zip(headers, colw):
        ax.add_patch(plt.Rectangle((x, y0), w, rowh, color="#12304b", ec=COLORS["grid"], lw=1))
        ax.text(x + 0.01, y0 + rowh / 2, h, color=COLORS["text"], fontsize=11, weight="bold", va="center")
        x += w
    for r, row in enumerate(rows):
        y = y0 - (r + 1) * rowh
        x = x0
        for c, (txt, w) in enumerate(zip(row, colw)):
            fill = COLORS["panel"] if r % 2 == 0 else "#0a1727"
            ax.add_patch(plt.Rectangle((x, y), w, rowh, color=fill, ec=COLORS["grid"], lw=0.8))
            color = COLORS.get(row[0], COLORS["text"]) if c == 0 else COLORS["text"]
            if c == 5 and "Avoid" in txt:
                color = COLORS["danger"]
            elif c == 5 and ("Best" in txt or "Use" in txt):
                color = COLORS["accent2"]
            ax.text(x + 0.01, y + rowh / 2, txt, color=color, fontsize=10.5, va="center", weight="bold" if c in (0, 5) else "normal")
            x += w
    return save_fig(fig, "boss_06_decision_matrix.png")


def chart_architecture():
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    ax.axis("off")
    fig.set_facecolor(COLORS["bg"])
    ax.set_facecolor(COLORS["bg"])
    ax.text(0.03, 0.93, "What the benchmark really measured", color=COLORS["text"], fontsize=20, weight="bold")
    boxes = [
        (0.08, 0.62, 0.18, 0.16, "GPU KV", "fast path"),
        (0.38, 0.62, 0.22, 0.16, "Host pinned L2", "hides SSD"),
        (0.72, 0.62, 0.2, 0.16, "L3 file SSD", "reload path"),
    ]
    for x, y, w, h, title, sub in boxes:
        ax.add_patch(plt.Rectangle((x, y), w, h, color=COLORS["panel"], ec=COLORS["accent"], lw=2))
        ax.text(x + w / 2, y + h * 0.62, title, color=COLORS["text"], fontsize=15, weight="bold", ha="center", va="center")
        ax.text(x + w / 2, y + h * 0.30, sub, color=COLORS["muted"], fontsize=12, ha="center", va="center")
    for a, b in [((0.26, 0.70), (0.38, 0.70)), ((0.60, 0.70), (0.72, 0.70))]:
        ax.annotate("", xy=b, xytext=a, arrowprops=dict(arrowstyle="->", color=COLORS["accent2"], lw=3))
    ax.text(0.08, 0.40, "Normal cold/warm test", color=COLORS["warn"], fontsize=15, weight="bold")
    ax.text(0.08, 0.34, "Mostly GPU prefill + L2 hit\nSSD differences disappear", color=COLORS["muted"], fontsize=12)
    ax.text(0.55, 0.40, "Phase7 multiprompt replay", color=COLORS["accent2"], fontsize=15, weight="bold")
    ax.text(0.55, 0.34, "20 prompts evict p0 from L2\nreplay p0 exposes L3 SSD", color=COLORS["muted"], fontsize=12)
    ax.add_patch(plt.Rectangle((0.53, 0.28), 0.41, 0.20, fill=False, ec=COLORS["accent2"], lw=2, linestyle="--"))
    return save_fig(fig, "boss_07_architecture.png")


def chart_scorecard(lat, io):
    fig, ax = plt.subplots(figsize=(11.5, 6.2))
    fig.set_facecolor(COLORS["bg"])
    ax.set_facecolor(COLORS["bg"])
    ax.axis("off")
    metrics = {
        "L3 latency": {"BIWIN": 5, "ZHITAI": 4, "WDC": 3, "Seagate": 2},
        "Stability": {"BIWIN": 5, "ZHITAI": 3.5, "WDC": 4, "Seagate": 1.5},
        "IO tail": {"BIWIN": 5, "ZHITAI": 4, "WDC": 2.5, "Seagate": 2},
        "Deployment": {"BIWIN": 3, "ZHITAI": 4, "WDC": 3, "Seagate": 3},
    }
    ax.text(0.03, 0.94, "Boss-level scorecard", color=COLORS["text"], fontsize=20, weight="bold")
    ax.text(0.03, 0.88, "5 = best. BIWIN is fastest path; ZHITAI is best independent data SSD.", color=COLORS["muted"], fontsize=12)
    y0 = 0.72
    for i, d in enumerate(DISKS):
        y = y0 - i * 0.14
        ax.text(0.05, y, d, color=COLORS[d], fontsize=15, weight="bold", va="center")
        x = 0.22
        for m in metrics:
            score = metrics[m][d]
            ax.add_patch(plt.Rectangle((x, y - 0.035), 0.18, 0.07, color="#0a1727", ec=COLORS["grid"], lw=1))
            ax.add_patch(plt.Rectangle((x, y - 0.035), 0.18 * score / 5, 0.07, color=COLORS[d], alpha=0.9))
            ax.text(x + 0.19, y, f"{score:.1f}", color=COLORS["text"], fontsize=10, va="center")
            if i == 0:
                ax.text(x, y + 0.075, m, color=COLORS["muted"], fontsize=10, ha="left")
            x += 0.20
    return save_fig(fig, "boss_08_scorecard.png")


def generate_charts():
    lat = load_latency_summary()
    io = load_io_summary()
    return {
        "ranking": chart_exec_ranking(lat),
        "single_multi": chart_single_vs_multi(lat),
        "io_shape": chart_io_latency(io),
        "util_tail": chart_util_vs_peak(io),
        "funnel": chart_evidence_funnel(),
        "decision": chart_decision_matrix(lat, io),
        "arch": chart_architecture(),
        "scorecard": chart_scorecard(lat, io),
    }


EMU_PER_IN = 914400
SLIDE_W = int(13.333333 * EMU_PER_IN)
SLIDE_H = int(7.5 * EMU_PER_IN)


def emu(inches: float) -> int:
    return int(inches * EMU_PER_IN)


@dataclass
class TextBox:
    x: float
    y: float
    w: float
    h: float
    text: str
    size: int = 24
    color: str = "edf6ff"
    bold: bool = False
    align: str = "l"


@dataclass
class Picture:
    path: Path
    x: float
    y: float
    w: float
    h: float


@dataclass
class Shape:
    x: float
    y: float
    w: float
    h: float
    color: str
    line: str = "27415f"


@dataclass
class Slide:
    title: str
    subtitle: str = ""
    textboxes: list[TextBox] | None = None
    pictures: list[Picture] | None = None
    shapes: list[Shape] | None = None
    notes: list[str] | None = None


def esc(s: str) -> str:
    return escape(s)


def tx_body(text: str, size: int, color: str, bold: bool, align: str) -> str:
    lines = text.split("\n")
    paras = []
    for line in lines:
        paras.append(
            f'<a:p><a:pPr algn="{align}"/>'
            f'<a:r><a:rPr lang="zh-CN" sz="{size*100}" dirty="0" b="{str(bold).lower()}">'
            f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
            f'</a:rPr><a:t>{esc(line)}</a:t></a:r></a:p>'
        )
    return '<a:txBody><a:bodyPr wrap="square"/><a:lstStyle/>' + "".join(paras) + "</a:txBody>"


def shape_xml(idx: int, shp: Shape) -> str:
    return f"""
    <p:sp>
      <p:nvSpPr><p:cNvPr id="{idx}" name="Shape {idx}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
      <p:spPr>
        <a:xfrm><a:off x="{emu(shp.x)}" y="{emu(shp.y)}"/><a:ext cx="{emu(shp.w)}" cy="{emu(shp.h)}"/></a:xfrm>
        <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
        <a:solidFill><a:srgbClr val="{shp.color}"/></a:solidFill>
        <a:ln w="12700"><a:solidFill><a:srgbClr val="{shp.line}"/></a:solidFill></a:ln>
      </p:spPr>
      <a:txBody><a:bodyPr/><a:lstStyle/><a:p/></a:txBody>
    </p:sp>"""


def textbox_xml(idx: int, tb: TextBox) -> str:
    return f"""
    <p:sp>
      <p:nvSpPr><p:cNvPr id="{idx}" name="TextBox {idx}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
      <p:spPr><a:xfrm><a:off x="{emu(tb.x)}" y="{emu(tb.y)}"/><a:ext cx="{emu(tb.w)}" cy="{emu(tb.h)}"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></p:spPr>
      {tx_body(tb.text, tb.size, tb.color, tb.bold, tb.align)}
    </p:sp>"""


def pic_xml(idx: int, rel_id: str, pic: Picture) -> str:
    return f"""
    <p:pic>
      <p:nvPicPr><p:cNvPr id="{idx}" name="{esc(pic.path.name)}"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
      <p:blipFill><a:blip r:embed="{rel_id}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
      <p:spPr><a:xfrm><a:off x="{emu(pic.x)}" y="{emu(pic.y)}"/><a:ext cx="{emu(pic.w)}" cy="{emu(pic.h)}"/></a:xfrm>
        <a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
    </p:pic>"""


def slide_xml(slide: Slide, rels: list[tuple[str, str]]) -> str:
    elements = []
    idx = 2
    elements.append(shape_xml(idx, Shape(0, 0, 13.333, 7.5, "07111f", "07111f"))); idx += 1
    elements.append(textbox_xml(idx, TextBox(0.45, 0.28, 12.4, 0.55, slide.title, 28, "edf6ff", True))); idx += 1
    if slide.subtitle:
        elements.append(textbox_xml(idx, TextBox(0.48, 0.86, 12.0, 0.35, slide.subtitle, 13, "9fb5c8"))); idx += 1
    for shp in slide.shapes or []:
        elements.append(shape_xml(idx, shp)); idx += 1
    for tb in slide.textboxes or []:
        elements.append(textbox_xml(idx, tb)); idx += 1
    for pic, (rid, _) in zip(slide.pictures or [], rels):
        elements.append(pic_xml(idx, rid, pic)); idx += 1
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="07111f"/></a:solidFill></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr>
      {''.join(elements)}
    </p:spTree>
  </p:cSld>
  <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>"""


def rels_xml(rels: list[tuple[str, str]]) -> str:
    body = []
    for rid, target in rels:
        body.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{target}"/>')
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(body) + "</Relationships>"


def build_slides(charts):
    return [
        Slide(
            "AI SSD × HiCache 选型预研",
            "给老板版: 结论、实验设计、IO 证据链与采购建议 | 2026-06-26",
            textboxes=[
                TextBox(0.65, 1.55, 5.7, 0.7, "结论先行", 30, "7cffb2", True),
                TextBox(0.68, 2.25, 5.8, 2.2, "BIWIN 系统盘路径最快\nZHITAI 是最佳独立 NTFS 数据盘\nWDC 稳定居中\nSeagate 有 bimodal 慢读风险", 22, "edf6ff", True),
                TextBox(0.68, 5.15, 5.8, 0.9, "核心判断: 选型不能看普通 cold/warm,必须看 Phase7 replay + 多 run tail。", 18, "ffcc66", True),
                TextBox(7.1, 1.65, 4.9, 2.7, "数据基础\n• 7 phases + v3 mount-fixed 重跑\n• Phase7 G: 6 run × 4 盘 = 24 replay 点\n• fio 12 个硬件极限点\n• iostat 1s 粒度 IO 画像\n• 13 张 profiling 图", 18, "edf6ff"),
            ],
            shapes=[Shape(0.45, 1.35, 6.2, 4.8, "0e1b2d"), Shape(6.85, 1.35, 5.7, 4.8, "0e1b2d")],
        ),
        Slide(
            "Executive ranking: 6-run mean, not single-run peak",
            "Selection-grade metric = replay_p0 mean + CV + IO diagnosis.",
            pictures=[Picture(charts["ranking"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "Why the test is credible",
            "The workflow deliberately separates cache validation, hardware ceiling, and real L3 reload.",
            pictures=[Picture(charts["funnel"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "What the benchmark really measured",
            "Normal cold/warm is an L2-hit test; Phase7 replay is the L3 SSD test.",
            pictures=[Picture(charts["arch"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "Single-run ranking is misleading",
            "v3 alone made Seagate look fine; 6 runs expose its tail risk.",
            pictures=[Picture(charts["single_multi"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "IO diagnosis: HiCache replay is latency-bound",
            "Replay is ~60-125KB reads, not a 1MB sequential fio workload.",
            pictures=[Picture(charts["io_shape"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "Disks are not saturated",
            "Peak bandwidth is not the main bottleneck; p99 await and reader behavior dominate.",
            pictures=[Picture(charts["util_tail"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "Decision matrix",
            "Recommendation uses observed latency, stability and IO behavior.",
            pictures=[Picture(charts["decision"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "Scorecard for purchase decision",
            "Fastest path and best data SSD are different decisions.",
            pictures=[Picture(charts["scorecard"], 0.65, 1.25, 12.0, 5.75)],
        ),
        Slide(
            "Recommended action",
            "What to buy, what to avoid, what to test next.",
            shapes=[Shape(0.55, 1.3, 3.9, 4.9, "0e1b2d"), Shape(4.72, 1.3, 3.9, 4.9, "0e1b2d"), Shape(8.9, 1.3, 3.9, 4.9, "0e1b2d")],
            textboxes=[
                TextBox(0.85, 1.65, 3.3, 0.4, "BUY / USE", 22, "7cffb2", True, "ctr"),
                TextBox(0.82, 2.25, 3.35, 2.6, "1. BIWIN as system/root L3 path when lowest latency matters\n2. ZHITAI as preferred independent NTFS data SSD\n3. WDC as stable capacity fallback", 18, "edf6ff"),
                TextBox(5.02, 1.65, 3.3, 0.4, "AVOID", 22, "ff6b7a", True, "ctr"),
                TextBox(5.00, 2.25, 3.35, 2.6, "Do not let Seagate alone carry tail-sensitive hot L3.\nIt had 3 slow runs around 3.4-3.5s out of 6.", 18, "edf6ff"),
                TextBox(9.2, 1.65, 3.3, 0.4, "NEXT", 22, "00d4ff", True, "ctr"),
                TextBox(9.18, 2.25, 3.35, 2.6, "1. Trace Seagate slow reads with blk_mq + smartctl\n2. Retest ZHITAI with drop_caches between runs\n3. Test sglang 0.6+ page-size / reader changes", 18, "edf6ff"),
                TextBox(0.75, 6.55, 11.8, 0.45, "Executive takeaway: add host RAM first, then use ZHITAI/WDC for data L3; optimize reader/page-size before overpaying for peak SSD bandwidth.", 16, "ffcc66", True, "ctr"),
            ],
        ),
    ]


def content_types(num_slides: int, media_names: list[str]) -> str:
    media_defaults = '<Default Extension="png" ContentType="image/png"/>'
    overrides = [
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
    ]
    for i in range(1, num_slides + 1):
        overrides.append(f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'{media_defaults}'
        + "".join(overrides)
        + "</Types>"
    )


def presentation_xml(num_slides: int) -> str:
    sld_ids = []
    for i in range(1, num_slides + 1):
        sld_ids.append(f'<p:sldId id="{255+i}" r:id="rId{i}"/>')
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId{num_slides+1}"/></p:sldMasterIdLst>
 <p:sldIdLst>{''.join(sld_ids)}</p:sldIdLst>
 <p:sldSz cx="{SLIDE_W}" cy="{SLIDE_H}" type="wide"/>
 <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>"""


def presentation_rels(num_slides: int) -> str:
    rels = []
    for i in range(1, num_slides + 1):
        rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>')
    rels.append(f'<Relationship Id="rId{num_slides+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>')
    rels.append(f'<Relationship Id="rId{num_slides+2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>')
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(rels) + "</Relationships>"


MINIMAL_MASTER = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst><p:sldLayoutId id="1" r:id="rId1"/></p:sldLayoutIdLst><p:txStyles><p:titleStyle/><p:bodyStyle/><p:otherStyle/></p:txStyles></p:sldMaster>"""

MINIMAL_LAYOUT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank">
<p:cSld name="Blank"><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/><a:chOff x="0" y="0"/><a:chExt cx="0" cy="0"/></a:xfrm></p:grpSpPr></p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>"""

MINIMAL_THEME = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="AI SSD Dark">
<a:themeElements><a:clrScheme name="dark"><a:dk1><a:srgbClr val="07111F"/></a:dk1><a:lt1><a:srgbClr val="EDF6FF"/></a:lt1><a:dk2><a:srgbClr val="0E1B2D"/></a:dk2><a:lt2><a:srgbClr val="9FB5C8"/></a:lt2><a:accent1><a:srgbClr val="00D4FF"/></a:accent1><a:accent2><a:srgbClr val="7CFFB2"/></a:accent2><a:accent3><a:srgbClr val="FFCC66"/></a:accent3><a:accent4><a:srgbClr val="FF6B7A"/></a:accent4><a:accent5><a:srgbClr val="27415F"/></a:accent5><a:accent6><a:srgbClr val="12304B"/></a:accent6><a:hlink><a:srgbClr val="00D4FF"/></a:hlink><a:folHlink><a:srgbClr val="7CFFB2"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Aptos Display"/></a:majorFont><a:minorFont><a:latin typeface="Aptos"/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst/><a:lnStyleLst/><a:effectStyleLst/><a:bgFillStyleLst/></a:fmtScheme></a:themeElements></a:theme>"""


def build_pptx(slides: list[Slide]):
    media_files: list[Path] = []
    slide_rels: list[list[tuple[str, str]]] = []
    for slide in slides:
        rels = []
        for pic in slide.pictures or []:
            media_name = f"image{len(media_files)+1}.png"
            media_files.append(pic.path)
            rels.append((f"rId{len(rels)+1}", media_name))
        slide_rels.append(rels)

    with zipfile.ZipFile(OUT, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types(len(slides), [p.name for p in media_files]))
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/></Relationships>')
        z.writestr("ppt/presentation.xml", presentation_xml(len(slides)))
        z.writestr("ppt/_rels/presentation.xml.rels", presentation_rels(len(slides)))
        z.writestr("ppt/slideMasters/slideMaster1.xml", MINIMAL_MASTER)
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/></Relationships>')
        z.writestr("ppt/slideLayouts/slideLayout1.xml", MINIMAL_LAYOUT)
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>')
        z.writestr("ppt/theme/theme1.xml", MINIMAL_THEME)
        for i, slide in enumerate(slides, start=1):
            z.writestr(f"ppt/slides/slide{i}.xml", slide_xml(slide, slide_rels[i-1]))
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", rels_xml(slide_rels[i-1]))
        for media_name, path in zip([rel[1] for rels in slide_rels for rel in rels], media_files):
            z.write(path, f"ppt/media/{media_name}")


def main():
    charts = generate_charts()
    slides = build_slides(charts)
    build_pptx(slides)
    print(f"Wrote {OUT}")
    print(f"Assets in {ASSETS}")


if __name__ == "__main__":
    main()
