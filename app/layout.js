import './globals.css';

export const metadata = {
  title: 'ERACLOUD 2.0 — Automate. Scale. Elevate.',
  description: 'Premium digital systems company for modern businesses.'
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
