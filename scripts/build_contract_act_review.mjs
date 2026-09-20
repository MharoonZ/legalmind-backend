/** Create a reviewer-friendly Excel workbook from the immutable draft JSON.
 *
 * The workbook is a review copy.  Editing a cell here does not silently alter
 * evidence in the database: corrected text must first be incorporated into a
 * new corpus version, which receives new content hashes and evidence IDs.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const corpus = JSON.parse(
  await fs.readFile(path.join(repoRoot, "data", "contract_act_1872_sections_1_30.json"), "utf8"),
);
const outputDir = path.join(repoRoot, "outputs", "haroon-sprint1-20260919");
const outputPath = path.join(outputDir, "contract_act_1872_sections_1_30.xlsx");
const qaDir = path.join(repoRoot, "tmp", "workbook_qa");

const workbook = Workbook.create();
const sections = workbook.worksheets.add("Sections");
const passages = workbook.worksheets.add("Page Passages");
const source = workbook.worksheets.add("Source");
const ink = "#1F2937";
const header = "#243B53";
const pale = "#F4F7FA";
const font = "Arial";

/** Apply a compact shared format without hiding editable source text. */
function formatSheet(sheet, lastColumn, lastRow) {
  sheet.showGridLines = false;
  sheet.getRange(`A1:${lastColumn}${lastRow}`).format.font = {
    name: font,
    size: 10,
    color: ink,
  };
  sheet.getRange(`A1:${lastColumn}1`).format.font = {
    name: font,
    size: 15,
    bold: true,
    color: ink,
  };
  sheet.getRange(`A4:${lastColumn}4`).format = {
    fill: header,
    font: { name: font, size: 10, bold: true, color: "#FFFFFF" },
    verticalAlignment: "center",
  };
  sheet.getRange(`A4:${lastColumn}4`).format.rowHeight = 30;
  sheet.freezePanes.freezeRows(4);
}

sections.getRange("A1").values = [["Contract Act sections 1 to 30"]];
sections.getRange("A2").values = [["PDF extraction for line-by-line legal review; page numbers refer to the PDF, not the contents pages."]];
sections.getRange("A4:I4").values = [[
  "Section", "Title", "PDF first page", "PDF last page", "Extracted section text",
  "Illustrations", "Review status", "Reviewer", "Review notes",
]];
sections.getRange("A5:I34").values = corpus.sections.map((section) => [
  section.section_number,
  section.title,
  section.pdf_page_start,
  section.pdf_page_end,
  section.text,
  /\bIllustrations?\b/.test(section.text) ? "Included" : "None shown",
  "Needs review",
  "",
  "",
]);
formatSheet(sections, "I", 34);
sections.getRange("A5:D34").format.verticalAlignment = "top";
sections.getRange("E5:I34").format.verticalAlignment = "top";
sections.getRange("E5:E34").format.wrapText = true;
sections.getRange("I5:I34").format.wrapText = true;
sections.getRange("A:A").format.columnWidth = 10;
sections.getRange("B:B").format.columnWidth = 72;
sections.getRange("C:D").format.columnWidth = 17;
sections.getRange("E:E").format.columnWidth = 120;
sections.getRange("F:F").format.columnWidth = 17;
sections.getRange("G:G").format.columnWidth = 20;
sections.getRange("H:H").format.columnWidth = 25;
sections.getRange("I:I").format.columnWidth = 42;
sections.getRange("A5:D34").setNumberFormat("0");
sections.getRange("G5:G34").dataValidation = {
  rule: { type: "list", values: ["Needs review", "Reviewed", "Correction needed"] },
};
for (let index = 0; index < corpus.sections.length; index += 1) {
  // Excel rows have a height limit.  Wider text columns keep even long
  // provisions readable while the formula bar retains the entire cell text.
  const section = corpus.sections[index];
  const estimatedLines = Math.ceil(section.text.length / 115) + section.text.split("\n").length;
  sections.getRange(`A${index + 5}:I${index + 5}`).format.rowHeight = Math.min(390, Math.max(54, estimatedLines * 13));
}
sections.tables.add("A4:I34", true, "ContractActSections");

const passageRows = corpus.sections.flatMap((section) =>
  section.passages.map((passage) => [
    section.section_number,
    section.title,
    passage.page_number,
    passage.text,
    passage.evidence_id,
  ]),
);
passages.getRange("A1").values = [["Page evidence passages"]];
passages.getRange("A2").values = [["Each row is one section fragment on one PDF page; the evidence ID changes if its text is corrected."]];
passages.getRange("A4:E4").values = [["Section", "Title", "PDF page", "Extracted page text", "Evidence ID"]];
passages.getRange(`A5:E${passageRows.length + 4}`).values = passageRows;
formatSheet(passages, "E", passageRows.length + 4);
passages.getRange("A:A").format.columnWidth = 10;
passages.getRange("B:B").format.columnWidth = 72;
passages.getRange("C:C").format.columnWidth = 14;
passages.getRange("D:D").format.columnWidth = 120;
passages.getRange("E:E").format.columnWidth = 74;
passages.getRange(`D5:D${passageRows.length + 4}`).format.wrapText = true;
passages.getRange(`A5:E${passageRows.length + 4}`).format.verticalAlignment = "top";
for (let index = 0; index < passageRows.length; index += 1) {
  const text = passageRows[index][3];
  const estimatedLines = Math.ceil(text.length / 115) + text.split("\n").length;
  passages.getRange(`A${index + 5}:E${index + 5}`).format.rowHeight = Math.min(390, Math.max(54, estimatedLines * 13));
}
passages.tables.add(`A4:E${passageRows.length + 4}`, true, "ContractActPagePassages");

source.getRange("A1").values = [["Source record"]];
source.getRange("A2").values = [["Review provenance before approving any text for user-facing answers."]];
source.getRange("A4:B4").values = [["Field", "Value"]];
source.getRange("A5:B14").values = [
  ["Law", corpus.law_name],
  ["Jurisdiction", corpus.jurisdiction],
  ["Official PDF URL", corpus.source_url],
  ["PDF SHA-256", corpus.pdf_sha256],
  ["PDF pages", corpus.pdf_page_count],
  ["Corpus SHA-256", corpus.corpus_sha256],
  ["Corpus version ID", corpus.source_id],
  ["Review status", "Needs review"],
  ["Reviewer", ""],
  ["Review date", ""],
];
source.getRange("A16").values = [["Review notes"]];
source.getRange("B16").values = [[
  "Check the PDF against every section, especially page breaks, illustrations, amendment marks, and printed line-end hyphens. The Pakistan Code site says its content is under review.",
]];
source.getRange("A17:B17").values = [[
  "Scope exception",
  "Inserted section 19A is on PDF page 18. It is separate from section 19 and excluded from the 30 numbered rows pending a supervisor decision. Sections 30A to 30C follow section 30 and are also excluded.",
]];
formatSheet(source, "B", 17);
source.freezePanes.unfreeze();
source.getRange("A:A").format.columnWidth = 23;
source.getRange("B:B").format.columnWidth = 110;
source.getRange("B5:B17").format.wrapText = true;
source.getRange("A5:B17").format.verticalAlignment = "top";
source.getRange("A5:B14").format.rowHeight = 30;
source.getRange("A16:B17").format.rowHeight = 80;
source.getRange("A5:B14").format.fill = pale;
source.getRange("B14").setNumberFormat("yyyy-mm-dd");

await fs.mkdir(outputDir, { recursive: true });
await fs.mkdir(qaDir, { recursive: true });
// Inspect representative source/section ranges and all sheets before export.
for (const [sheetName, range] of [
  ["Sections", "A4:I7"],
  ["Page Passages", "A4:E7"],
  ["Source", "A4:B14"],
]) {
  const report = await workbook.inspect({
    kind: "table",
    range: `${sheetName}!${range}`,
    include: "values",
    tableMaxRows: 4,
    tableMaxCols: 9,
    maxChars: 2200,
  });
  console.log(report.ndjson);
}
for (const sheetName of ["Sections", "Page Passages", "Source"]) {
  const preview = await workbook.render({
    sheetName,
    range: sheetName === "Source" ? "A1:B17" : sheetName === "Sections" ? "A1:I6" : "A1:E6",
    scale: 1,
    format: "png",
  });
  await fs.writeFile(
    path.join(qaDir, `${sheetName.toLowerCase().replaceAll(" ", "_")}_preview.png`),
    new Uint8Array(await preview.arrayBuffer()),
  );
}
const errorReport = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!",
  options: { useRegex: true, maxResults: 20 },
  summary: "formula error scan",
});
console.log(errorReport.ndjson);
const file = await SpreadsheetFile.exportXlsx(workbook);
await file.save(outputPath);
// Reopen the final Excel file.  This catches export errors that would not
// appear in the in-memory preview or pre-export inspection.
const savedWorkbook = await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath));
const savedSections = savedWorkbook.worksheets.getItem("Sections").getRange("A5:A34").values;
const savedPassages = savedWorkbook.worksheets.getItem("Page Passages").getRange("A5:A45").values;
const savedPdfHash = savedWorkbook.worksheets.getItem("Source").getRange("B8").values[0][0];
if (
  savedSections.length !== 30 ||
  savedSections.some((row, index) => row[0] !== index + 1) ||
  savedPassages.length !== 41 ||
  savedPdfHash !== corpus.pdf_sha256
) {
  throw new Error("Exported workbook failed its source and row-count checks");
}
console.log(`Saved ${outputPath}`);
