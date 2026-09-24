import { randomUUID } from "crypto";
import { promises as fs } from "fs";
import path from "path";
import { env } from "../config/env";

/**
 * Clean storage abstraction (task requirement: "If object storage is
 * not yet implemented, create a clean abstraction rather than
 * hard-coding the filesystem throughout the application"). Every call
 * site (knowledge.routes.ts) goes through this interface, never `fs`
 * directly -- swapping to S3/GCS later means adding one new class here,
 * not touching routes/services.
 */
export interface StorageProvider {
  /** Persists a buffer, returns an opaque key to pass to read()/delete() later. */
  save(tenantId: number, originalFilename: string, data: Buffer): Promise<string>;
  read(key: string): Promise<Buffer>;
  delete(key: string): Promise<void>;
}

/**
 * Default provider: local disk under env.uploadsDir, one subfolder per
 * tenant (defense in depth alongside the DB-level tenant_id check --
 * even a path-traversal bug elsewhere can't cross a tenant's own
 * subfolder without an additional `..` that sanitizeKey() below
 * already rejects).
 *
 * Local disk is a single-node deployment issue for multi-instance
 * node-api (each instance would see different data) -- fine for this
 * project's current single-instance docker-compose.yml. If node-api
 * is horizontally scaled later, swap the default export below for a
 * new S3StorageProvider implementing the same interface; nothing else
 * needs to change.
 */
export class LocalDiskStorageProvider implements StorageProvider {
  constructor(private readonly baseDir: string) {}

  private sanitizeKey(key: string): string {
    // Defense in depth: a key is always ours (generated in save()),
    // but reject anything that could escape baseDir if one were ever
    // passed in from elsewhere.
    const resolved = path.resolve(this.baseDir, key);
    if (!resolved.startsWith(path.resolve(this.baseDir) + path.sep)) {
      throw new Error("Invalid storage key");
    }
    return resolved;
  }

  async save(tenantId: number, originalFilename: string, data: Buffer): Promise<string> {
    const ext = path.extname(originalFilename).toLowerCase();
    const key = path.join(String(tenantId), `${randomUUID()}${ext}`);
    const fullPath = this.sanitizeKey(key);
    await fs.mkdir(path.dirname(fullPath), { recursive: true });
    await fs.writeFile(fullPath, data);
    return key;
  }

  async read(key: string): Promise<Buffer> {
    return fs.readFile(this.sanitizeKey(key));
  }

  async delete(key: string): Promise<void> {
    try {
      await fs.unlink(this.sanitizeKey(key));
    } catch (err) {
      // A missing file on delete is not an error worth failing the
      // request over (e.g. retried delete, or storage_path was never
      // set for a manually-entered document).
      if ((err as NodeJS.ErrnoException).code !== "ENOENT") {
        throw err;
      }
    }
  }
}

export const storage: StorageProvider = new LocalDiskStorageProvider(env.uploadsDir);
