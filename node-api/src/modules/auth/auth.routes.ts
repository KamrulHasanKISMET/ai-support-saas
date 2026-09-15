import { Router } from "express";
import bcrypt from "bcryptjs";
import jwt from "jsonwebtoken";
import { pool } from "../../config/database";
import { env } from "../../config/env";
import { AppError } from "../../middleware/errorHandler";
import { generateApiKey } from "../../utils/crypto";
import { authRepository } from "./auth.repository";

export const authRouter = Router();

interface JwtClaims {
  userId: number;
  tenantId: number;
  role: string;
}

function issueToken(claims: JwtClaims): string {
  return jwt.sign(claims, env.jwtSecret, { expiresIn: "7d" });
}

/**
 * Self-serve signup: creates a new tenant AND its first (owner) user in
 * one call, returns both a dashboard JWT and the tenant's api_key
 * (channel adapters/webhooks use the api_key; dashboard UI uses the JWT
 * — see middleware/auth.ts for how each is verified).
 */
authRouter.post("/signup", async (req, res, next) => {
  const client = await pool.connect();
  try {
    const { businessName, slug, email, password } = req.body as {
      businessName: string;
      slug: string;
      email: string;
      password: string;
    };

    if (!businessName || !slug || !email || !password) {
      throw new AppError(
        "VALIDATION_ERROR",
        "businessName, slug, email and password are required."
      );
    }
    if (password.length < 8) {
      throw new AppError(
        "VALIDATION_ERROR",
        "Password must be at least 8 characters."
      );
    }

    await client.query("BEGIN");

    const apiKey = generateApiKey();
    const tenantResult = await client.query(
      `INSERT INTO tenants (name, slug, api_key) VALUES ($1, $2, $3) RETURNING *`,
      [businessName, slug, apiKey]
    );
    const tenant = tenantResult.rows[0];

    const passwordHash = await bcrypt.hash(password, 12);
    const userResult = await client.query(
      `INSERT INTO users (tenant_id, email, password_hash, role)
       VALUES ($1, $2, $3, 'owner') RETURNING *`,
      [tenant.id, email.toLowerCase(), passwordHash]
    );
    const user = userResult.rows[0];

    await client.query("COMMIT");

    const token = issueToken({
      userId: user.id,
      tenantId: tenant.id,
      role: user.role,
    });

    res.status(201).json({
      tenant: { id: tenant.id, name: tenant.name, slug: tenant.slug, apiKey: tenant.api_key },
      user: { id: user.id, email: user.email, role: user.role },
      token,
    });
  } catch (err) {
    await client.query("ROLLBACK");
    if ((err as { code?: string }).code === "23505") {
      return next(new AppError("CONFLICT", "That slug or email is already in use.", 409));
    }
    next(err);
  } finally {
    client.release();
  }
});

/** Dashboard login. tenantSlug scopes the email lookup since emails are
 * only unique per-tenant (see users table's UNIQUE(tenant_id, email)). */
authRouter.post("/login", async (req, res, next) => {
  try {
    const { tenantSlug, email, password } = req.body as {
      tenantSlug: string;
      email: string;
      password: string;
    };

    if (!tenantSlug || !email || !password) {
      throw new AppError(
        "VALIDATION_ERROR",
        "tenantSlug, email and password are required."
      );
    }

    const user = await authRepository.findUserByEmail(tenantSlug, email);
    if (!user) {
      throw new AppError("INVALID_CREDENTIALS", "Invalid email or password.", 401);
    }

    const valid = await bcrypt.compare(password, user.password_hash);
    if (!valid) {
      throw new AppError("INVALID_CREDENTIALS", "Invalid email or password.", 401);
    }

    const token = issueToken({
      userId: user.id,
      tenantId: user.tenant_id,
      role: user.role,
    });

    res.json({
      user: { id: user.id, email: user.email, role: user.role },
      token,
    });
  } catch (err) {
    next(err);
  }
});
