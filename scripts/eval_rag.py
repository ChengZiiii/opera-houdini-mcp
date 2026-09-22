#!/usr/bin/env python
"""eval_rag.py — RAG 检索质量评测门禁（versioned-rag-index §4，design D6）。

纯 stdlib、不依赖 hou、任意进程可跑。对指定索引跑 gold set 评测，输出
MRR / hit@1/5/10 + per-query 明细 + 领域分组指标；可与既有基线对比（可
比性指纹判定），门禁双层判据 + gold_missing 守卫 + 双向告警。**告警不
阻断**（索引仍可用，构建流程只输出明显警告）。

## 指标语义（脚本内自足定义，spec rag-eval-gate）

- 检索走 ``_rag`` 生产同参路径：``_rag.tokenize(query)`` →
  ``BM25Index.search(tokens, limit=10)``（与 search_docs 的 limit=10
  语义一致，score 降序、同分 doc_id 升序稳定排序）。
- 多 gold 命中任一即 hit；RR = 首个命中排名的倒数；hit@k = 前k名内
  命中（k ∈ {1,5,10}）。
- ``gold_missing``：条目的全部 gold 路径都不在被测索引文档集中 → 计
  missing，**剔除出 MRR 分母**（5-zip 局部索引遇 solaris 条目即此形
  态）；分母逃逸是隐性虚高通道，报告单列有效分母与 missing 清单。
- 领域分组（整体 + 每 domain）：防单领域回退被整体均值稀释。

## 可比性判定（操作定义，design D6）

gold set hash 相同 且 zip 集差异仅含新增（无移除/改名）→ 可比；否则
报告显式声明不可比，差异 zip 单列。

## 门禁判据（双层 + 双向）

1. 整体 MRR 降幅 > 0.05 → 红灯；
2. per-query 层：可比条目中 RR 降幅 > 0.3 的占比 ≥ 25% → 红灯
   （n≈24 时单条彻底失败仅动 MRR 0.042，纯整体阈值对灾难级单点回退
   静默；本层捕获 BM25/tokenize 变更的「多条小幅位移」形态）；
3. gold_missing > 2 条 或 > 10% → 红灯（绝对守卫，无需基线）；
4. MRR 显著升高（> +0.10）→ 黄灯（gold 污染/分母逃逸的典型表现）。

## 双落点存档（spec）

``--out-dir``（默认 ``<index 目录>/eval/``）产 eval_report.md +
eval_report.json；``--write-baseline`` 产小体积基线 JSON（随 submodule
入库，防重装丢失，供跨版本对比）。

运行示例：
    python scripts/eval_rag.py --index ~/.opera-houdini-mcp/rag/21.0.596/index.v1.json
    python scripts/eval_rag.py --index <idx> --baseline scripts/rag_baseline.json
    python scripts/eval_rag.py --index <idx> --write-baseline scripts/rag_baseline.json
"""

import argparse
import datetime
import hashlib
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

try:
    import _rag
except ImportError:
    sys.path.insert(0, _HERE)
    import _rag

# ---------------------------------------------------------------------------
# 常量：判据阈值（design D6；改动需同步 spec rag-eval-gate）
# ---------------------------------------------------------------------------
RETRIEVAL_LIMIT = 10          # 生产 search_docs 默认 limit 同参
MRR_DROP_WARN = 0.05          # 整体 MRR 降幅红灯阈值
MRR_RISE_WARN = 0.10          # MRR 异常升高黄灯阈值（污染典型表现）
RR_DROP_PER_QUERY = 0.3       # per-query RR 降幅判据
PER_QUERY_SHARE_WARN = 0.25   # 上述条目占比告警阈值
GOLD_MISSING_MAX_COUNT = 2    # gold_missing 绝对守卫
GOLD_MISSING_MAX_RATIO = 0.10

BASELINE_SCHEMA = "houdinimcp.rag-eval-baseline"
BASELINE_VERSION = 1
PARSER_ID = "eval_rag.py/1 + _rag.HoudiniTokenizer"

FORM_NATURAL = "natural"
FORM_TITLE = "title"
_VALID_FORMS = (FORM_NATURAL, FORM_TITLE)


def _utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# gold set 加载与校验
# ---------------------------------------------------------------------------
def load_gold(path):
    """加载并校验 gold set：list[{query, gold[], domain, form, note,
    verified_on}]。形态合法值 natural|title；结构不合法直接抛 ValueError
    （门禁数据坏了必须大声失败，不能静默跑完）。"""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list) or not data:
        raise ValueError("gold set must be a non-empty list")
    seen_queries = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError("gold[%d] must be object" % i)
        for key in ("query", "gold", "domain", "form"):
            if key not in entry:
                raise ValueError("gold[%d] missing field %r" % (i, key))
        if not isinstance(entry["query"], str) or not entry["query"].strip():
            raise ValueError("gold[%d].query must be non-empty str" % i)
        if entry["query"] in seen_queries:
            raise ValueError("gold[%d].query duplicate: %r"
                             % (i, entry["query"]))
        seen_queries.add(entry["query"])
        if (not isinstance(entry["gold"], list) or not entry["gold"]
                or not all(isinstance(g, str) and g for g in entry["gold"])):
            raise ValueError("gold[%d].gold must be non-empty list of str"
                             % i)
        if not isinstance(entry["domain"], str) or not entry["domain"]:
            raise ValueError("gold[%d].domain must be str" % i)
        if entry["form"] not in _VALID_FORMS:
            raise ValueError("gold[%d].form must be one of %s"
                             % (i, ",".join(_VALID_FORMS)))
    return data


def gold_sha256(gold_entries):
    """gold set 内容 hash（canonical 序列化：仅判据相关字段，排序稳定）。"""
    canonical = json.dumps(
        [{"query": e["query"], "gold": sorted(e["gold"]),
          "domain": e["domain"], "form": e["form"]}
         for e in gold_entries],
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 评测主逻辑
# ---------------------------------------------------------------------------
def evaluate(index_path, gold_entries):
    """对索引跑全量 gold 评测。返回 report dict（metrics + per_query +
    domains + fingerprint）。**不修改被测索引文件**。"""
    status = _rag.load_index(index_path)
    if status["state"] != "ok":
        raise ValueError("index unusable (%s): %s"
                         % (status["state"], status.get("reason")))
    index = status["index"]

    # 顶层元数据（指纹用）：重读原始 JSON 的 meta 字段
    with open(index_path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    zips = raw.get("zips")
    if not isinstance(zips, list):
        zips = []
    fingerprint = {
        "gold_sha256": gold_sha256(gold_entries),
        "gold_count": len(gold_entries),
        "zips": sorted(zips),
        "built_at": raw.get("built_at", ""),
        "document_count": raw.get("document_count", index.document_count),
        "source": raw.get("source", ""),
        "parser_id": PARSER_ID,
    }

    known_paths = set(index.docs_by_id[d]["path"]
                      for d in index.docs_by_id)

    per_query = []
    valid_rrs = []
    domains = {}
    for entry in gold_entries:
        gold = set(entry["gold"])
        present = gold & known_paths
        record = {
            "query": entry["query"],
            "domain": entry["domain"],
            "form": entry["form"],
            "gold_missing": not present,
            "missing_paths": sorted(gold - known_paths),
            "rank": None,
            "rr": 0.0,
            "hit1": False, "hit5": False, "hit10": False,
            "first_hit_path": None,
        }
        if present:
            tokens = _rag.tokenize(entry["query"])
            results = index.search(tokens, limit=RETRIEVAL_LIMIT)
            for pos, (doc_id, _score) in enumerate(results, start=1):
                path = index.docs_by_id[doc_id]["path"]
                if path in gold:
                    record["rank"] = pos
                    record["rr"] = 1.0 / pos
                    record["first_hit_path"] = path
                    record["hit1"] = pos <= 1
                    record["hit5"] = pos <= 5
                    record["hit10"] = pos <= 10
                    break
            valid_rrs.append(record["rr"])
        per_query.append(record)

        bucket = domains.setdefault(
            entry["domain"],
            {"total": 0, "valid": 0, "rr_sum": 0.0,
             "hit1": 0, "hit5": 0, "hit10": 0, "missing": 0})
        bucket["total"] += 1
        if record["gold_missing"]:
            bucket["missing"] += 1
        else:
            bucket["valid"] += 1
            bucket["rr_sum"] += record["rr"]
            bucket["hit1"] += 1 if record["hit1"] else 0
            bucket["hit5"] += 1 if record["hit5"] else 0
            bucket["hit10"] += 1 if record["hit10"] else 0

    valid_count = len(valid_rrs)
    total_count = len(gold_entries)
    metrics = {
        "total_entries": total_count,
        "valid_entries": valid_count,
        "gold_missing": total_count - valid_count,
        "gold_missing_ratio": (
            (total_count - valid_count) / float(total_count)
            if total_count else 0.0),
        "mrr": (sum(valid_rrs) / valid_count) if valid_count else 0.0,
        "hit1": _rate(per_query, "hit1"),
        "hit5": _rate(per_query, "hit5"),
        "hit10": _rate(per_query, "hit10"),
    }
    domain_metrics = {}
    for name, bucket in domains.items():
        domain_metrics[name] = {
            "total": bucket["total"], "valid": bucket["valid"],
            "missing": bucket["missing"],
            "mrr": (bucket["rr_sum"] / bucket["valid"])
                   if bucket["valid"] else 0.0,
            "hit1": bucket["hit1"] / float(bucket["valid"])
                    if bucket["valid"] else 0.0,
        }
    return {
        "schema": BASELINE_SCHEMA,
        "version": BASELINE_VERSION,
        "evaluated_at": _utc_now_iso(),
        "index_path": os.path.abspath(index_path),
        "fingerprint": fingerprint,
        "metrics": metrics,
        "domains": domain_metrics,
        "per_query": per_query,
    }


def _rate(per_query, key):
    valid = [r for r in per_query if not r["gold_missing"]]
    if not valid:
        return 0.0
    return sum(1 for r in valid if r[key]) / float(len(valid))


# ---------------------------------------------------------------------------
# 基线对比与门禁判据
# ---------------------------------------------------------------------------
def comparability(current_fp, baseline_fp):
    """可比性操作定义：gold hash 相同 且 zip 差异仅新增。

    返回 {"comparable": bool, "reason": str, "zip_diff": {...}}。
    """
    result = {"comparable": False, "reason": "", "zip_diff": {}}
    if current_fp.get("gold_sha256") != baseline_fp.get("gold_sha256"):
        result["reason"] = "gold set hash mismatch (edit/removed/added)"
        return result
    cur = set(current_fp.get("zips") or [])
    base = set(baseline_fp.get("zips") or [])
    added = sorted(cur - base)
    removed = sorted(base - cur)
    result["zip_diff"] = {"added": added, "removed": removed}
    if removed:
        result["reason"] = (
            "zip set removed/renamed vs baseline: %s" % ",".join(removed))
        return result
    if added:
        result["reason"] = (
            "comparable with additions only: %s" % ",".join(added))
    else:
        result["reason"] = "identical fingerprint inputs"
    result["comparable"] = True
    return result


def gate_alarms(report, baseline=None):
    """门禁判据（design D6）。返回 alarm dict 列表：
    {"level": "red"|"yellow", "code", "message"}。不抛、不阻断。"""
    alarms = []
    metrics = report["metrics"]
    # 3) gold_missing 绝对守卫（无需基线）
    if (metrics["gold_missing"] > GOLD_MISSING_MAX_COUNT
            or metrics["gold_missing_ratio"] > GOLD_MISSING_MAX_RATIO):
        alarms.append({
            "level": "red", "code": "gold_missing",
            "message": "gold_missing=%d/%d (%.0f%%) exceeds guard "
                       "(>%d or >%d%%); effective denominator shrunk — "
                       "check zip coverage / path drift"
                       % (metrics["gold_missing"],
                          metrics["total_entries"],
                          metrics["gold_missing_ratio"] * 100,
                          GOLD_MISSING_MAX_COUNT,
                          GOLD_MISSING_MAX_RATIO * 100)})
    if baseline is None:
        return alarms
    comp = comparability(report["fingerprint"], baseline["fingerprint"])
    if not comp["comparable"]:
        alarms.append({
            "level": "yellow", "code": "not_comparable",
            "message": "baseline not comparable (%s); metrics are "
                       "informational only" % comp["reason"]})
        return alarms
    base_metrics = baseline["metrics"]
    delta = base_metrics["mrr"] - metrics["mrr"]
    # 1) 整体 MRR 降幅
    if delta > MRR_DROP_WARN:
        alarms.append({
            "level": "red", "code": "mrr_drop",
            "message": "overall MRR %.4f -> %.4f (drop %.4f > %.2f)"
                       % (base_metrics["mrr"], metrics["mrr"], delta,
                          MRR_DROP_WARN)})
    # 4) 双向：异常升高黄灯
    rise = metrics["mrr"] - base_metrics["mrr"]
    if rise > MRR_RISE_WARN:
        alarms.append({
            "level": "yellow", "code": "mrr_suspicious_rise",
            "message": "overall MRR rose %.4f (> +%.2f): check gold "
                       "contamination / denominator escape"
                       % (rise, MRR_RISE_WARN)})
    # 2) per-query 层：RR 降幅 > 0.3 占比 ≥ 25%
    base_rr = {r["query"]: r["rr"] for r in baseline["per_query"]
               if not r["gold_missing"]}
    drops = []
    for record in report["per_query"]:
        if record["gold_missing"]:
            continue
        base_value = base_rr.get(record["query"])
        if base_value is None:
            continue
        if (base_value - record["rr"]) > RR_DROP_PER_QUERY:
            drops.append(record["query"])
    matched = sum(1 for r in report["per_query"]
                  if not r["gold_missing"] and r["query"] in base_rr)
    if matched and len(drops) / float(matched) >= PER_QUERY_SHARE_WARN:
        alarms.append({
            "level": "red", "code": "per_query_regression",
            "message": "%d/%d comparable queries dropped RR > %.1f "
                       "(>= %.0f%%): %s"
                       % (len(drops), matched, RR_DROP_PER_QUERY,
                          PER_QUERY_SHARE_WARN * 100, "; ".join(drops))})
    return alarms


# ---------------------------------------------------------------------------
# 报告渲染（Markdown + JSON 双输出）
# ---------------------------------------------------------------------------
def render_markdown(report, baseline, comp, alarms):
    lines = []
    fp = report["fingerprint"]
    m = report["metrics"]
    lines.append("# RAG Eval Report")
    lines.append("")
    lines.append("- evaluated_at: %s" % report["evaluated_at"])
    lines.append("- index: `%s`" % report["index_path"])
    lines.append("- gold: %d entries (sha256 `%s`)"
                 % (fp["gold_count"], fp["gold_sha256"][:16]))
    lines.append("- zips: %d" % len(fp["zips"]))
    lines.append("- document_count: %s / built_at: %s"
                 % (fp["document_count"], fp["built_at"]))
    lines.append("- parser: %s" % fp["parser_id"])
    if comp is not None:
        lines.append("- baseline comparability: %s (%s)"
                     % ("COMPARABLE" if comp["comparable"]
                        else "NOT COMPARABLE", comp["reason"]))
        if comp["zip_diff"]:
            lines.append("  - zip diff: +%s / -%s"
                         % (comp["zip_diff"].get("added", []),
                            comp["zip_diff"].get("removed", [])))
    lines.append("")
    lines.append("## Alarms")
    if alarms:
        for alarm in alarms:
            lines.append("- **[%s] %s**: %s"
                         % (alarm["level"].upper(), alarm["code"],
                            alarm["message"]))
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Overall (effective denominator = %d valid / %d total; "
                 "gold_missing excluded from MRR)"
                 % (m["valid_entries"], m["total_entries"]))
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    lines.append("| MRR | %.4f |" % m["mrr"])
    lines.append("| hit@1 | %.4f |" % m["hit1"])
    lines.append("| hit@5 | %.4f |" % m["hit5"])
    lines.append("| hit@10 | %.4f |" % m["hit10"])
    lines.append("| gold_missing | %d (%.1f%%) |"
                 % (m["gold_missing"], m["gold_missing_ratio"] * 100))
    lines.append("")
    lines.append("## By domain")
    lines.append("")
    lines.append("| domain | total | valid | missing | MRR | hit@1 |")
    lines.append("|---|---|---|---|---|---|")
    for name in sorted(report["domains"]):
        d = report["domains"][name]
        lines.append("| %s | %d | %d | %d | %.4f | %.4f |"
                     % (name, d["total"], d["valid"], d["missing"],
                        d["mrr"], d["hit1"]))
    lines.append("")
    lines.append("## Per query")
    lines.append("")
    lines.append("| query | form | domain | rank | RR | missing |")
    lines.append("|---|---|---|---|---|---|")
    for record in report["per_query"]:
        rank = str(record["rank"]) if record["rank"] else "-"
        lines.append("| %s | %s | %s | %s | %.4f | %s |"
                     % (record["query"], record["form"], record["domain"],
                        rank, record["rr"],
                        "yes" if record["gold_missing"] else ""))
    missing_list = [r for r in report["per_query"] if r["gold_missing"]]
    if missing_list:
        lines.append("")
        lines.append("## gold_missing detail (NOT in MRR denominator)")
        for record in missing_list:
            lines.append("- `%s` [%s/%s]: %s"
                         % (record["query"], record["domain"],
                            record["form"],
                            "; ".join(record["missing_paths"])))
    if baseline is not None and comp is not None and comp["comparable"]:
        lines.append("")
        lines.append("## Baseline delta")
        bm = baseline["metrics"]
        lines.append("")
        lines.append("| metric | baseline | current | delta |")
        lines.append("|---|---|---|---|")
        lines.append("| MRR | %.4f | %.4f | %+.4f |"
                     % (bm["mrr"], m["mrr"], m["mrr"] - bm["mrr"]))
        lines.append("| hit@10 | %.4f | %.4f | %+.4f |"
                     % (bm["hit10"], m["hit10"], m["hit10"] - bm["hit10"]))
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evaluate RAG retrieval quality against a gold set "
                    "(versioned-rag-index eval gate).")
    parser.add_argument("--index", required=True,
                        help="path to index.v1.json to evaluate")
    parser.add_argument(
        "--gold", default=os.path.join(_HERE, "rag_gold_set.json"),
        help="gold set json (default: scripts/rag_gold_set.json)")
    parser.add_argument("--baseline", default=None,
                        help="baseline json to compare against")
    parser.add_argument("--out-dir", default=None,
                        help="report output dir (default: <index dir>/eval/)")
    parser.add_argument("--write-baseline", default=None,
                        help="write current metrics as baseline json")
    args = parser.parse_args(argv)

    try:
        gold = load_gold(args.gold)
    except (OSError, ValueError) as exc:
        sys.stderr.write("eval_rag: bad gold set: %s\n" % exc)
        return 2
    natural = sum(1 for e in gold if e["form"] == FORM_NATURAL)
    natural_ratio = natural / float(len(gold))
    if natural_ratio < 0.60:
        # spec 硬约束：自然语言式 ≥60%（防对索引键查索引键的送分）
        sys.stderr.write(
            "eval_rag: natural-language queries %.0f%% < 60%% (%d/%d)\n"
            % (natural_ratio * 100, natural, len(gold)))
        return 2

    try:
        report = evaluate(args.index, gold)
    except (OSError, ValueError) as exc:
        sys.stderr.write("eval_rag: evaluation failed: %s\n" % exc)
        return 3

    baseline = None
    comp = None
    if args.baseline:
        try:
            with open(args.baseline, "r", encoding="utf-8") as handle:
                baseline = json.load(handle)
            comp = comparability(report["fingerprint"],
                                 baseline.get("fingerprint", {}))
        except (OSError, ValueError) as exc:
            sys.stderr.write("eval_rag: baseline unreadable (%s); "
                             "skipping comparison\n" % exc)
            baseline = None

    alarms = gate_alarms(report, baseline)

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.index)), "eval")
    try:
        os.makedirs(out_dir, exist_ok=True)
        md_path = os.path.join(out_dir, "eval_report.md")
        json_path = os.path.join(out_dir, "eval_report.json")
        with open(md_path, "w", encoding="utf-8") as handle:
            handle.write(render_markdown(report, baseline, comp, alarms))
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except OSError as exc:
        sys.stderr.write("eval_rag: write report failed: %s\n" % exc)
        return 4

    if args.write_baseline:
        slim = dict(report)
        try:
            with open(args.write_baseline, "w", encoding="utf-8") as handle:
                json.dump(slim, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except OSError as exc:
            sys.stderr.write("eval_rag: write baseline failed: %s\n" % exc)
            return 4

    metrics = report["metrics"]
    sys.stdout.write(
        "eval_rag: MRR=%.4f hit@1/5/10=%.4f/%.4f/%.4f valid=%d/%d "
        "missing=%d\n" % (metrics["mrr"], metrics["hit1"], metrics["hit5"],
                          metrics["hit10"], metrics["valid_entries"],
                          metrics["total_entries"], metrics["gold_missing"]))
    for alarm in alarms:
        sys.stdout.write("eval_rag: [%s] %s: %s\n"
                         % (alarm["level"].upper(), alarm["code"],
                            alarm["message"]))
    sys.stdout.write("eval_rag: report -> %s\n" % out_dir)
    # 告警不阻断（spec：门禁不阻断构建）
    return 0


if __name__ == "__main__":
    sys.exit(main())
