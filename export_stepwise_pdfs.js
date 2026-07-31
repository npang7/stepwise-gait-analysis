const path = require("path");
const { pathToFileURL } = require("url");

const { chromium } = require("playwright");

const root = path.resolve("stepwise_batch_reports");
const reportFiles = [
  ["user_report.html", "user_report.pdf"],
  ["data_guide.html", "data_guide.pdf"],
  ["report.html", "report.pdf"],
];

async function main() {
  const fs = require("fs");
  const chromeCandidates = [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ];
  const executablePath = chromeCandidates.find((candidate) => fs.existsSync(candidate));
  const browser = await chromium.launch({ headless: true, executablePath });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1600 } });

  const dirs = fs
    .readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .map((entry) => path.join(root, entry.name));

  for (const dir of dirs) {
    for (const [htmlName, pdfName] of reportFiles) {
      const htmlPath = path.join(dir, htmlName);
      if (!fs.existsSync(htmlPath)) continue;
      const pdfPath = path.join(dir, pdfName);
      await page.goto(pathToFileURL(htmlPath).href, { waitUntil: "networkidle" });
      await page.pdf({
        path: pdfPath,
        format: "A4",
        printBackground: true,
        margin: { top: "12mm", right: "10mm", bottom: "12mm", left: "10mm" },
      });
      console.log(`PDF exported: ${pdfPath}`);
    }
  }

  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
