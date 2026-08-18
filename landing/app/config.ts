export const TRAXION_APP_URL =
  "https://frontend-staging-9498.up.railway.app/login";

export const HYPERLIQUID_REFERRAL_URL =
  "https://app.hyperliquid.xyz/join/DIGITALEMPOWER";

export const HYPERLIQUID_API_WALLET_URL =
  "https://app.hyperliquid.xyz/API";

export const TRAXION_ASSET_BASE_PATH =
  process.env.NEXT_PUBLIC_BASE_PATH?.replace(/\/$/, "") ?? "";

export const traxionAsset = (path: string) =>
  `${TRAXION_ASSET_BASE_PATH}${path.startsWith("/") ? path : `/${path}`}`;

export const TRAXION_CANONICAL_URL =
  process.env.NEXT_PUBLIC_TRAXION_CANONICAL_URL ?? "[DOMINIO DEFINITIVO]";
export const PRIVACY_POLICY_URL = "[URL PRIVACY POLICY]";
export const TERMS_URL = "[URL TERMINI E CONDIZIONI]";
export const CONTACT_EMAIL = "[EMAIL CONTATTO]";
