import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Donor Management",
  description: "Nonprofit consulting donor management platform",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-screen bg-background font-sans antialiased">
        {children}
      </body>
    </html>
  );
}
