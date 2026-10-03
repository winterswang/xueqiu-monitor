#!/usr/bin/env node
// 雪球每日流程巡检 —— 核对本轮改动在**真实定时任务**里是否生效。
//
// 协议（宿主 preRunHook）:
//   exit 0 → 放行：发现问题，叫醒 agent 诊断
//   exit 2 → 跳过本轮：一切正常，不烧 token、不出声
//   其它非零 → fail-closed：脚本自身出错，宿主阻止本轮并记录
//
// 模式由触发时段自判（XQ_PRECHECK_MODE 可覆盖，便于手工自测）:
//   < 12 点  → morning  09:00：只看 08:00 crawler 那轮的产出
//   12–18 点 → health   14:00：只看 13:30 健康检查
//   >= 18 点 → eod      20:30：全天汇总，逐个核对产物
//
// hermes 各任务本来就会推飞书卡片，所以这里不重复报「任务跑没跑」，
// 只查卡片不会告诉你的具体信号 —— 也就是这轮改动的落点。

import { readFileSync, existsSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { homedir } from 'node:os';

const now = new Date();
const mode = process.env.XQ_PRECHECK_MODE
  || (now.getHours() < 12 ? 'morning' : now.getHours() < 18 ? 'health' : 'eod');

const CRAWLER = process.env.XQ_CRAWLER_DIR || join(homedir(), 'code/claude_code/xueqiu-crawler');
const MONITOR = process.env.XQ_MONITOR_DIR || join(homedir(), 'code/claude_code/xueqiu-monitor');

const problems = [];
let complete = true;
const today = now.toLocaleDateString('sv-SE');
const dayOf = (d) => d.toLocaleDateString('sv-SE');

// crawler：今天的日报有没有产出。
// #54 修的「无新增也产出最小日报」落在这里：以前 0 新增 return ""，
// 下游 publish/push 全报「日报文件不存在」，当天零交付。
function checkCrawler() {
  const statsPath = join(CRAWLER, 'data/.last_crawl_stats.json');
  try {
    const stats = JSON.parse(readFileSync(statsPath, 'utf8'));
    if (stats.date !== today) {
      problems.push(`crawler 今天没跑：stats.date=${stats.date}，期望 ${today}`);
    } else if (Number(stats.failed) > 0) {
      problems.push(`crawler ${stats.failed}/${stats.total_users} 个账号失败`);
    }
  } catch (err) {
    complete = false;
    problems.push(`读不到 .last_crawl_stats.json: ${err.message}`);
  }
  const reportPath = join(CRAWLER, `data/daily_reports/${today}.md`);
  if (!existsSync(reportPath)) {
    problems.push(`今天没有日报文件：${reportPath} —— 查 generate_report 的「无新增」分支`);
  }
}

// monitor 健康检查。
// #46 的落点：source_failures 按「最近一轮 pipeline」窗口算；ok 分支的 detail
// 里必须带「窗口 ….」时间。注意 warn 分支的文案（N 次失败 → …）本来就没有
// 「窗口」二字 —— 那是真实失败，不是口径问题，别误报。
function checkHealth() {
  try {
    const out = execFileSync('python3', [join(MONITOR, 'scripts/health_check.py')], {
      encoding: 'utf8', timeout: 180000, cwd: MONITOR,
    });
    const rep = JSON.parse(out);
    if (rep.status !== 'ok') problems.push(`health_check status=${rep.status}（期望 ok）`);
    const sf = (rep.checks || []).find((c) => c.check === 'source_failures');
    if (sf && String(sf.detail).startsWith('读取失败')) {
      problems.push(`source_failures 读取失败：${sf.detail}（#46 修掉的 NameError 复发）`);
    } else if (sf && sf.status === 'ok' && !String(sf.detail).includes('窗口')) {
      problems.push(`source_failures 报 ok 却没有窗口时间，可能没走到新口径：${sf.detail}`);
    }
  } catch (err) {
    complete = false;
    problems.push(`health_check 执行失败: ${err.message}`);
  }
}

// 全天汇总：逐个核对所有定时任务的产物。
function checkEod() {
  checkCrawler();
  // 六个 pipeline 日志由 13:00 / 14:00–18:00 六条 cron 各自 tee **覆盖**写入，
  // 所以「今天更新过 + 有 [SUMMARY]」= 今天跑完了。
  for (const name of ['pipeline.log', 'pipeline-g1.log', 'pipeline-g2.log',
                      'pipeline-g3.log', 'pipeline-g4.log', 'pipeline-g5.log']) {
    const p = join(MONITOR, 'logs', name);
    if (!existsSync(p)) { problems.push(`缺日志 ${name}`); continue; }
    if (dayOf(statSync(p).mtime) !== today) {
      problems.push(`${name} 今天没更新（最后 ${dayOf(statSync(p).mtime)}）`);
      continue;
    }
    const s = readFileSync(p, 'utf8').match(/\[SUMMARY\] (.+)/);
    if (!s) { problems.push(`${name} 今天跑了但没有 [SUMMARY]（可能中途失败）`); continue; }
    const failed = /failed=(\d+)/.exec(s[1]);
    if (failed && Number(failed[1]) > 0) problems.push(`${name} 有 ${failed[1]} 只失败：${s[1]}`);
  }
  const syncLog = join(MONITOR, 'logs/sync_watchlist.log');
  if (!existsSync(syncLog)) {
    problems.push('缺 logs/sync_watchlist.log（12:00 的 sync-watchlist 没跑过？）');
  } else if (dayOf(statSync(syncLog).mtime) !== today) {
    problems.push(`sync_watchlist.log 今天没更新（最后 ${dayOf(statSync(syncLog).mtime)}）`);
  }
}

if (mode === 'morning') checkCrawler();
else if (mode === 'health') checkHealth();
else checkEod();

if (complete) process.stdout.write('CINDY_PRECHECK_OK\n');

if (problems.length === 0) {
  console.log(`[每日流程巡检:${mode}] 全部正常`);
  process.exit(2);
}
console.log(`[每日流程巡检:${mode}] 发现问题：`);
for (const p of problems) console.log('  - ' + p);
process.exit(0);
