import type { Metadata } from "next";
import "./globals.css";
import { TRAXION_CANONICAL_URL } from "./config";

const canonicalIsConfigured = TRAXION_CANONICAL_URL.startsWith("https://");

export const metadata: Metadata = {
  title: "TRAXION | Intelligenza ibrida. Esecuzione deterministica.",
  description:
    "TRAXION collega analisi umana, Capital Intelligence AI, Risk Engine ed esecuzione disciplinata su Hyperliquid.",
  applicationName: "TRAXION",
  keywords: [
    "TRAXION",
    "trading ibrido",
    "Capital Intelligence AI",
    "Risk Engine",
    "Hyperliquid",
  ],
  openGraph: {
    type: "website",
    locale: "it_IT",
    title: "TRAXION | Intelligenza ibrida. Esecuzione deterministica.",
    description:
      "Un sistema di trading ibrido che struttura l'intelligence e applica regole deterministiche, controllabili e verificabili.",
    siteName: "TRAXION",
    images: [
      {
        url: "/traxion-ai-copy-trading-logo.webp",
        width: 1200,
        height: 400,
        alt: "TRAXION",
      },
    ],
  },
  robots: { index: true, follow: true },
  alternates: canonicalIsConfigured
    ? { canonical: TRAXION_CANONICAL_URL }
    : undefined,
  other: {
    "codex-preview": "development",
    "referrer": "strict-origin-when-cross-origin",
  },
  icons: {
    icon: "/traxion-tab-icon-v3.ico",
    shortcut: "/traxion-tab-icon-v3.ico",
    apple: "/traxion-touch-icon-v3.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="it">
      <body>{children}</body>
    </html>
  );
}
