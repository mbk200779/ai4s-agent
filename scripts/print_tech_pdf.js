/**
 * 把 docs/.build/tech-doc.html 打印成 A4 PDF（带页脚页码）。
 *
 * 依赖：puppeteer-core（`npm i puppeteer-core`）+ 本机 Edge/Chrome。
 * 之所以不用 HTML→PDF 的 Python 库：只需维护一份 CSS 排版规则，
 * 打印结果与浏览器所见一致；页码交给 footerTemplate，避开
 * Chromium 不支持 @page margin-box 的限制。
 *
 * 用法：
 *   python scripts/build_tech_pdf.py
 *   node scripts/print_tech_pdf.js
 */
const path = require('path');
const fs = require('fs');
const puppeteer = require('puppeteer-core');

const ROOT = path.resolve(__dirname, '..');
const HTML = path.join(ROOT, 'docs/.build/tech-doc.html');
const PDF = path.join(ROOT, 'docs/AI4S学术研究Agent-技术文档.pdf');

const CANDIDATES = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
];
const EDGE = process.env.CHROME_PATH || CANDIDATES.find((p) => fs.existsSync(p));

(async () => {
  if (!EDGE) {
    console.error('找不到 Edge / Chrome，请用 CHROME_PATH 指定可执行文件路径');
    process.exit(1);
  }
  if (!fs.existsSync(HTML)) {
    console.error('HTML 不存在，先跑 scripts/build_tech_pdf.py：' + HTML);
    process.exit(1);
  }

  const browser = await puppeteer.launch({
    executablePath: EDGE,
    headless: 'new',
    protocolTimeout: 180000,
    args: ['--no-sandbox', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  await page.goto('file:///' + HTML.replace(/\\/g, '/'), { waitUntil: 'load', timeout: 60000 });

  // 图片解码完成再打印，否则会出现空白图位
  await page.evaluate(async () => {
    const imgs = Array.from(document.images);
    await Promise.all(imgs.map((i) => (i.complete ? null : new Promise((r) => { i.onload = i.onerror = r; }))));
  });
  await new Promise((r) => setTimeout(r, 300));

  await page.pdf({
    path: PDF,
    format: 'A4',
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: '<div></div>',
    footerTemplate: `
      <div style="width:100%;font-family:'Microsoft YaHei',sans-serif;font-size:7.6pt;color:#8b9bb0;
                  padding:0 17mm;display:flex;justify-content:space-between;">
        <span>AI4S 学术研究 Agent · 技术文档 v1.0.0</span>
        <span><span class="pageNumber"></span> / <span class="totalPages"></span></span>
      </div>`,
    margin: { top: '17mm', bottom: '16mm', left: '17mm', right: '17mm' },
  });

  await browser.close();
  const kb = (fs.statSync(PDF).size / 1024).toFixed(0);
  console.log(`PDF 已生成：${PDF}  (${kb} KB)`);
})().catch((e) => { console.error('FAILED', e.message); process.exit(1); });
