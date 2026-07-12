#!/usr/bin/env node
import { basename } from "node:path";
import { readFile, writeFile } from "node:fs/promises";
import { getDocumentProxy, getMeta } from "unpdf";

const [, , pdfPath, outPath] = process.argv;

if (!pdfPath || !outPath) {
  console.error("Usage: node scripts/extract_unpdf_items.mjs <input.pdf> <output.json>");
  process.exit(2);
}

const data = new Uint8Array(await readFile(pdfPath));
const pdf = await getDocumentProxy(data);
const meta = await getMeta(pdf).catch((error) => ({ error: String(error) }));
const pages = [];

for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
  const page = await pdf.getPage(pageNumber);
  const viewport = page.getViewport({ scale: 1 });
  const textContent = await page.getTextContent({ includeMarkedContent: true });
  const items = textContent.items.map((item) => {
    if ("str" in item) {
      return {
        str: item.str,
        dir: item.dir,
        width: item.width,
        height: item.height,
        transform: item.transform,
        fontName: item.fontName,
        hasEOL: item.hasEOL === true
      };
    }
    return item;
  });

  pages.push({
    page: pageNumber,
    width: viewport.width,
    height: viewport.height,
    items,
    styles: textContent.styles
  });
}

await writeFile(
  outPath,
  JSON.stringify(
    {
      pdf: basename(pdfPath),
      totalPages: pdf.numPages,
      metadata: meta,
      pages
    },
    null,
    2
  )
);
