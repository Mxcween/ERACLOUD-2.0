'use client';

import { useEffect, useMemo, useState } from 'react';

const navItems = ['Services', 'Process', 'Pricing', 'Projects', 'Contact'];

const services = [
  ['Web Development', 'Conversion-ready websites with premium design, fast performance, and mobile-first structure.'],
  ['AI Automation', 'AI assistants, workflow automation, lead routing, and internal systems that reduce repetitive work.'],
  ['Branding & Visuals', 'Visual identity, banners, social assets, and premium brand direction for stronger trust.'],
  ['Social Media Foundation', 'Profile setup, highlights, visual system, content structure, and brand consistency.'],
  ['Growth Funnels', 'Landing pages, contact flows, lead capture, and clear conversion paths.'],
  ['Meta Ads Setup', 'Campaign structure, creative direction, audience setup, and launch guidance.']
];

const processSteps = [
  ['01 Discovery', 'We understand your business, goals, audience, and current online presence.'],
  ['02 Strategy', 'We define the offer, digital direction, and conversion path.'],
  ['03 Design', 'We create the visual system, website concept, and premium brand experience.'],
  ['04 Build', 'We develop the website, forms, automations, and digital foundation.'],
  ['05 Launch', 'We test, optimize, and publish everything smoothly.'],
  ['06 Support', 'We help improve, update, and scale after launch.']
];

const contactCards = [
  ['✉', 'Email', 'era.cloud.co@gmail.com', 'Best for detailed project briefs.', 'mailto:era.cloud.co@gmail.com', 'Send Email'],
  ['💬', 'WhatsApp', '+49 160 91408872', 'Quick direct line for launch-focused projects.', 'https://wa.me/4916091408872', 'Open WhatsApp'],
  ['📱', 'WhatsApp', '+49 151 53111186', 'For design + social media communication.', 'https://wa.me/4915153111186', 'Open WhatsApp'],
  ['📍', 'Location', 'Kempten, Germany', 'Remote-first delivery with premium support.', 'https://maps.google.com/?q=Kempten,Germany', 'View Map']
];

export default function HomePage() {
  const [menuOpen, setMenuOpen] = useState(false);
  const [sent, setSent] = useState(false);

  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) return;

    const root = document.documentElement;
    let x = window.innerWidth / 2;
    let y = window.innerHeight / 2;
    let tx = x;
    let ty = y;
    let raf = 0;

    const move = (e) => {
      tx = e.clientX;
      ty = e.clientY;
    };

    const loop = () => {
      x += (tx - x) * 0.08;
      y += (ty - y) * 0.08;
      root.style.setProperty('--mx', `${x}px`);
      root.style.setProperty('--my', `${y}px`);
      raf = requestAnimationFrame(loop);
    };

    window.addEventListener('pointermove', move);
    raf = requestAnimationFrame(loop);

    return () => {
      window.removeEventListener('pointermove', move);
      cancelAnimationFrame(raf);
    };
  }, []);

  const year = useMemo(() => new Date().getFullYear(), []);

  return (
    <main>
      <div className="cursor-glow" />
      <header className="header glass">
        <div className="logo">ERACLOUD</div>
        <button className="menu-toggle" type="button" aria-expanded={menuOpen} aria-label="Toggle navigation" onClick={() => setMenuOpen((prev) => !prev)}>☰</button>
        <nav className={`nav ${menuOpen ? 'open' : ''}`}>{navItems.map((item) => <a key={item} href={`#${item.toLowerCase()}`} onClick={() => setMenuOpen(false)}>{item}</a>)}</nav>
        <button className="cta">Start Project</button>
      </header>

      <section className="hero section">
        <div className="hero-left">
          <span className="badge">Premium Digital Systems</span>
          <h1>Automate.<br />Scale.<br /><span>Elevate.</span></h1>
          <p>ERACLOUD builds premium websites, AI automations, branded visuals, and digital systems that help businesses look better, work faster, and attract more clients.</p>
          <div className="hero-cta"><button className="cta">Start a Project</button><button className="ghost">View Services</button></div>
        </div>
        <div className="hero-right">
          <div className="logo-orb glass">
            <div className="shine" />
            <div className="floating-tag a">AI-powered workflows</div>
            <div className="floating-tag b">Built for modern businesses</div>
            <div className="floating-tag c">Premium digital presence</div>
            <div className="brand">ERACLOUD</div>
          </div>
        </div>
      </section>

      <section className="feature-strip glass section">{['Premium Websites', 'AI Automation', 'Branded Visuals', 'Growth Systems'].map((f) => <article key={f}><span>✦</span><p>{f}</p></article>)}</section>

      <section id="services" className="section">
        <h2>Everything your business needs to look premium online.</h2><p className="sub">From website to automation — we build the digital foundation that helps your business move faster and attract better clients.</p>
        <div className="grid">{services.map(([t, d]) => <article className="glass card luxe-card" key={t}><div className="icon-dot" /> <h3>{t}</h3><p>{d}</p><span>→</span></article>)}</div>
      </section>

      <section id="process" className="section">
        <h2>From idea to launch — clear, fast, premium.</h2>
        <div className="timeline">{processSteps.map(([t, d]) => <article className="glass step luxe-card" key={t}><h3>{t}</h3><p>{d}</p></article>)}</div>
      </section>

      <section id="pricing" className="section aura">
        <h2>Flexible pricing for modern businesses.</h2><p className="sub">Every project is different. We adapt to your budget, speed, complexity, and business goals.</p>
        <div className="prices">{[
          ['Starter Launch', 'from 250€', 'For small businesses that need a clean online foundation.', ['one-page landing page', 'basic branding polish', 'contact CTA', 'mobile responsive structure', 'social media visual foundation'], 'Start with Starter'],
          ['Growth System', 'from 500€', 'For businesses that need a stronger digital presence and better conversion flow.', ['premium website structure', 'social media setup', 'branded visuals', 'lead/contact funnel', 'basic SEO setup', 'Meta Ads setup guidance'], 'Build Growth System', 'Most requested'],
          ['Premium Experience', 'up to 1000€', 'For businesses that want a complete premium digital system.', ['high-end website', 'AI automation concept/setup', 'branded visuals', 'social media foundation', 'contact/request system', 'conversion-focused structure', 'launch support'], 'Create Premium Experience']
        ].map(([n, p, d, li, c, b]) => <article className={`glass price ${b ? 'featured' : ''}`} key={n}>{b && <small>{b}</small>}<h3>{n}</h3><h4>{p}</h4><p>{d}</p><ul>{li.map((i) => <li key={i}>{i}</li>)}</ul><button className={b ? 'cta' : 'ghost'}>{c}</button></article>)}</div>
      </section>

      <section id="contact" className="section inquiry-grid aura">
        <div className="inquiry-copy"><span className="badge">Premium application panel</span><h2>Apply for your digital system.</h2><p>Tell us what you want to build. We’ll review your request and show you the best next step for your business.</p></div>
        <form className="glass inquiry-form" onSubmit={(e) => { e.preventDefault(); setSent(true); }}>
          {['Name', 'Business name', 'Email', 'Phone / WhatsApp', 'Website or Instagram link'].map((f) => <label key={f}><span>{f}</span><input required={f === 'Name' || f === 'Email'} /></label>)}
          <label><span>Service needed</span><select defaultValue=""><option value="" disabled>Select a service</option><option>Website</option><option>AI Automation</option><option>Branding / Visuals</option><option>Social Media Setup</option><option>Meta Ads</option><option>Full Digital System</option></select></label>
          <label><span>Budget</span><select defaultValue=""><option value="" disabled>Select budget</option><option>250–500€</option><option>500–750€</option><option>750–1000€</option></select></label>
          <label className="full"><span>Project message</span><textarea rows={5} /></label>
          <button className="cta submit">Send Project Request</button><p className="trust">No pressure. Just send your idea — we’ll respond with a clear next step.</p>{sent && <p className="success">Request sent. We will contact you shortly.</p>}
        </form>
      </section>

      <section id="projects" className="section">
        <h2>Projects coming soon.</h2><p className="sub">Our first public transformations and case studies will be added soon.</p>
        <div className="grid projects-grid">{['Website transformations', 'AI automation systems', 'Brand visuals & growth'].map((item) => <article className="glass project-card" key={item}><span className="coming">Coming soon</span><div className="preview" /><h3>{item}</h3><p>Confidential previews locked while current launches are in production.</p></article>)}</div>
      </section>

      <section className="section contact concierge" id="contact-cards"><h2>Let’s build your digital system.</h2><p className="sub">Send us your idea — we’ll show you how it can look, work, and convert better.</p><div className="contact-grid">{contactCards.map(([i, l, v, d, href, cta]) => <article key={v} className="glass contact-card"><div className="label-row"><span className="emoji">{i}</span><span>{l}</span></div><h3>{v}</h3><p>{d}</p><a href={href} target="_blank" rel="noreferrer">{cta} ↗</a></article>)}</div></section>

      <footer className="section footer glass"><h3>ERACLOUD</h3><p>Automate. Scale. Elevate.</p><nav>{navItems.map((item) => <a key={item} href={`#${item.toLowerCase()}`}>{item}</a>)}</nav><small>Premium digital systems for modern businesses. © {year}</small></footer>
    </main>
  );
}
