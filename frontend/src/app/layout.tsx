import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'AuditAI - Autonomous Expense Auditor',
  description: 'AI-powered expense audit and policy compliance system',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen bg-gray-50">{children}</body>
    </html>
  );
}

