import { Request } from "express";

export interface RawBodyRequest extends Request {
  rawBody?: Buffer;
}

/**
 * Passed as express.json()'s `verify` option. Stashes the raw bytes
 * on the request before JSON.parse() ever touches them — signature
 * verification (whatsapp.signature.ts) MUST hash these exact bytes,
 * since re-serializing a parsed object can differ from the original
 * (key order, whitespace) and would make every signature check fail.
 */
export function captureRawBody(req: RawBodyRequest, _res: unknown, buf: Buffer): void {
  req.rawBody = buf;
}
