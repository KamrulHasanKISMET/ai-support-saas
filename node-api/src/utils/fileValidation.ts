/**
 * Upload validation for Knowledge Base documents. "Never trust the
 * client-provided MIME type alone" (task Phase 4) -- so every check
 * here layers: extension whitelist -> declared Content-Type sanity ->
 * magic-byte signature sniffed from the actual bytes. All three must
 * agree with the claimed format before a file is accepted.
 */

export type SupportedFormat = "pdf" | "docx" | "txt" | "csv";

const EXTENSION_TO_FORMAT: Record<string, SupportedFormat> = {
  ".pdf": "pdf",
  ".docx": "docx",
  ".txt": "txt",
  ".csv": "csv",
};

// Declared Content-Type values we accept for each format. Deliberately
// permissive here (many browsers/clients send inconsistent values for
// .txt/.csv) -- the magic-byte check below is the real gate for
// binary formats; text formats have no reliable magic bytes, so
// extension + a UTF-8 decodability check (see sniffFormat) carry more
// weight for those.
const ACCEPTED_MIME_TYPES: Record<SupportedFormat, string[]> = {
  pdf: ["application/pdf"],
  docx: [
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/zip", // some clients send this for .docx (it IS a zip container)
    "application/octet-stream",
  ],
  txt: ["text/plain", "application/octet-stream"],
  csv: ["text/csv", "application/vnd.ms-excel", "text/plain", "application/octet-stream"],
};

export const MAX_FILENAME_LENGTH = 255;

export class UploadValidationError extends Error {
  constructor(message: string) {
    super(message);
  }
}

function extensionOf(filename: string): string {
  const idx = filename.lastIndexOf(".");
  return idx === -1 ? "" : filename.slice(idx).toLowerCase();
}

/**
 * Sniffs the actual format from file bytes (magic numbers), independent
 * of whatever extension/Content-Type the client claimed.
 *   PDF  -> starts with "%PDF-"
 *   DOCX -> a ZIP container (PK\x03\x04) -- .xlsx/.pptx share this
 *           signature too, so this alone doesn't prove "is a Word doc",
 *           only "is a valid zip", which is what we can cheaply verify
 *           without a full OOXML parse.
 *   TXT/CSV -> no reliable magic bytes; accepted only if the buffer
 *           decodes as valid UTF-8 text (rejects e.g. a renamed .exe).
 */
function sniffFormat(data: Buffer): SupportedFormat | null {
  if (data.subarray(0, 5).toString("ascii") === "%PDF-") {
    return "pdf";
  }
  if (
    data.length >= 4 &&
    data[0] === 0x50 &&
    data[1] === 0x4b &&
    (data[2] === 0x03 || data[2] === 0x05 || data[2] === 0x07)
  ) {
    return "docx"; // zip container; extension/declared type narrows this to docx vs csv/txt below
  }
  return null;
}

function looksLikeUtf8Text(data: Buffer): boolean {
  try {
    const decoded = data.toString("utf-8");
    // A binary file decoded as UTF-8 typically contains the U+FFFD
    // replacement character or raw NUL bytes; real text files don't.
    return !decoded.includes("\uFFFD") && !decoded.includes("\u0000");
  } catch {
    return false;
  }
}

export interface ValidatedUpload {
  format: SupportedFormat;
  sanitizedFilename: string;
}

/**
 * Validates filename, declared MIME type, size, and actual content
 * bytes together. Throws UploadValidationError (caught by the route
 * and turned into a 400 AppError) on any mismatch.
 */
export function validateUpload(
  originalFilename: string,
  declaredMimeType: string,
  size: number,
  data: Buffer,
  maxSizeBytes: number
): ValidatedUpload {
  if (!originalFilename || originalFilename.length > MAX_FILENAME_LENGTH) {
    throw new UploadValidationError("Filename is missing or too long.");
  }
  // Strip any directory component a malicious/odd client might send
  // (defense in depth -- multer already gives us just the basename in
  // practice, but never trust client-supplied path segments).
  const sanitizedFilename = originalFilename.replace(/^.*[\\/]/, "");

  const ext = extensionOf(sanitizedFilename);
  const claimedFormat = EXTENSION_TO_FORMAT[ext];
  if (!claimedFormat) {
    throw new UploadValidationError(
      `Unsupported file extension "${ext}". Supported: .pdf, .docx, .txt, .csv`
    );
  }

  if (size <= 0 || size > maxSizeBytes) {
    throw new UploadValidationError(
      `File size ${size} bytes is invalid or exceeds the ${maxSizeBytes}-byte limit.`
    );
  }

  const acceptedMimes = ACCEPTED_MIME_TYPES[claimedFormat];
  if (declaredMimeType && !acceptedMimes.includes(declaredMimeType)) {
    throw new UploadValidationError(
      `Declared content type "${declaredMimeType}" does not match a ".${claimedFormat}" file.`
    );
  }

  if (claimedFormat === "pdf" || claimedFormat === "docx") {
    const sniffed = sniffFormat(data);
    if (sniffed !== claimedFormat) {
      throw new UploadValidationError(
        `File content does not match its ".${ext}" extension (failed signature check).`
      );
    }
  } else {
    // txt/csv: no binary signature to check; reject anything that
    // isn't plausible UTF-8 text (catches a renamed binary/executable).
    if (!looksLikeUtf8Text(data)) {
      throw new UploadValidationError(
        `File content for ".${ext}" does not look like valid text.`
      );
    }
  }

  return { format: claimedFormat, sanitizedFilename };
}
