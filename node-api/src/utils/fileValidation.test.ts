import { strict as assert } from "node:assert";
import { test } from "node:test";
import { validateUpload, UploadValidationError } from "./fileValidation";

const PDF_MAGIC = Buffer.from("%PDF-1.4\n%rest of a fake pdf\n");
const ZIP_MAGIC = Buffer.from([0x50, 0x4b, 0x03, 0x04, 0x00, 0x00]); // valid docx/zip signature
const PLAIN_TEXT = Buffer.from("Hello, this is plain UTF-8 text.");
const BINARY_GARBAGE = Buffer.from([0x00, 0xff, 0x00, 0xfe, 0x01, 0x02]);

const MAX_SIZE = 20 * 1024 * 1024;

test("accepts a valid PDF (extension + mime + magic bytes agree)", () => {
  const result = validateUpload("policy.pdf", "application/pdf", PDF_MAGIC.length, PDF_MAGIC, MAX_SIZE);
  assert.equal(result.format, "pdf");
  assert.equal(result.sanitizedFilename, "policy.pdf");
});

test("accepts a valid DOCX (zip magic bytes)", () => {
  const result = validateUpload(
    "catalog.docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ZIP_MAGIC.length,
    ZIP_MAGIC,
    MAX_SIZE
  );
  assert.equal(result.format, "docx");
});

test("accepts plain UTF-8 TXT", () => {
  const result = validateUpload("notes.txt", "text/plain", PLAIN_TEXT.length, PLAIN_TEXT, MAX_SIZE);
  assert.equal(result.format, "txt");
});

test("accepts UTF-8 CSV", () => {
  const csv = Buffer.from("name,price\nWidget,10\n");
  const result = validateUpload("prices.csv", "text/csv", csv.length, csv, MAX_SIZE);
  assert.equal(result.format, "csv");
});

test("rejects an unsupported extension", () => {
  assert.throws(
    () => validateUpload("script.exe", "application/octet-stream", 10, BINARY_GARBAGE, MAX_SIZE),
    UploadValidationError
  );
});

test("rejects a file whose extension doesn't match its declared MIME type", () => {
  assert.throws(
    () => validateUpload("policy.pdf", "image/png", PDF_MAGIC.length, PDF_MAGIC, MAX_SIZE),
    UploadValidationError
  );
});

test("rejects content that doesn't match its extension (renamed binary as .pdf)", () => {
  // Never trust the extension alone -- BINARY_GARBAGE has no PDF magic bytes.
  assert.throws(
    () => validateUpload("fake.pdf", "application/pdf", BINARY_GARBAGE.length, BINARY_GARBAGE, MAX_SIZE),
    UploadValidationError
  );
});

test("rejects a renamed binary claiming to be .txt", () => {
  assert.throws(
    () => validateUpload("fake.txt", "text/plain", BINARY_GARBAGE.length, BINARY_GARBAGE, MAX_SIZE),
    UploadValidationError
  );
});

test("rejects a file over the size limit", () => {
  assert.throws(
    () => validateUpload("big.txt", "text/plain", MAX_SIZE + 1, PLAIN_TEXT, MAX_SIZE),
    UploadValidationError
  );
});

test("rejects a zero-byte file", () => {
  assert.throws(
    () => validateUpload("empty.txt", "text/plain", 0, Buffer.alloc(0), MAX_SIZE),
    UploadValidationError
  );
});

test("rejects a missing filename", () => {
  assert.throws(
    () => validateUpload("", "text/plain", PLAIN_TEXT.length, PLAIN_TEXT, MAX_SIZE),
    UploadValidationError
  );
});

test("strips a client-supplied directory component from the filename (path traversal defense)", () => {
  const result = validateUpload(
    "../../etc/passwd.txt",
    "text/plain",
    PLAIN_TEXT.length,
    PLAIN_TEXT,
    MAX_SIZE
  );
  assert.equal(result.sanitizedFilename, "passwd.txt");
});

test("rejects an excessively long filename", () => {
  const longName = "a".repeat(300) + ".txt";
  assert.throws(
    () => validateUpload(longName, "text/plain", PLAIN_TEXT.length, PLAIN_TEXT, MAX_SIZE),
    UploadValidationError
  );
});
