const path = require('path');
const { pathToFileURL } = require('url');
const fs = require('fs');
const { chromium } = require('playwright');
(async () => {
  const executablePath = [
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
  ].find(p => fs.existsSync(p));
  const browser = await chromium.launch({ headless: true, executablePath });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1600 } });
  const dir = path.resolve('stepwise_batch_reports/experiment2');
  for (const [htmlName, pdfName] of [['user_report.html','user_report.pdf'], ['data_guide.html','data_guide.pdf'], ['report.html','report.pdf']]) {
    const htmlPath = path.join(dir, htmlName);
    await page.goto(pathToFileURL(htmlPath).href, { waitUntil: 'networkidle' });
    await page.pdf({ path: path.join(dir, pdfName), format: 'A4', printBackground: true, margin: {top:'12mm', right:'10mm', bottom:'12mm', left:'10mm'} });
    console.log(pdfName);
  }
  await browser.close();
})();
