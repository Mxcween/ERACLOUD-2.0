'use client';

import { useEffect, useMemo, useState } from 'react';

const navItems = [
  ['Services', 'services'],
  ['Process', 'process'],
  ['Pricing', 'pricing'],
  ['Projects', 'projects'],
  ['Contact', 'contact-cards']
];

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
  ['email', 'Email', 'era.cloud.co@gmail.com', 'Best for detailed project briefs.', 'mailto:era.cloud.co@gmail.com', 'Send email'],
  ['phone', 'Phone 1', '+49 160 91408872', 'Fast direct support for active projects.', 'tel:+4916091408872', 'Call now'],
  ['phone', 'Phone 2', '+49 151 53111186', 'Direct line for design and automation requests.', 'tel:+4915153111186', 'Call direct'],
  ['location', 'Location', 'Kempten, Germany', 'Remote-first execution with premium coordination.', 'https://maps.google.com/?q=Kempten,Germany', 'Open map']
];

const initialForm = { name: '', business: '', email: '', phone: '', site: '', service: '', budget: '', message: '' };

function BrandLogo() {
  return (
    <div className="brand-wrap" aria-label="ERACLOUD logo">
      <svg className="brand-svg" viewBox="0 0 220 90" role="img">
        <defs>
          <linearGradient id="ice" x1="0" x2="1">
            <stop offset="0%" stopColor="#ffffff" />
            <stop offset="55%" stopColor="#d4ebff" />
            <stop offset="100%" stopColor="#7eb2ff" />
          </linearGradient>
        </defs>
        <g>
          <rect x="6" y="8" width="74" height="74" rx="24" fill="rgba(255,255,255,0.28)" stroke="rgba(180,212,255,0.75)" />
          <circle cx="43" cy="45" r="22" fill="none" stroke="url(#ice)" strokeWidth="7" />
          <path d="M94 44h118" stroke="url(#ice)" strokeWidth="8" strokeLinecap="round" />
          <path d="M94 60h92" stroke="url(#ice)" strokeWidth="5" strokeLinecap="round" opacity=".88" />
        </g>
      </svg>
      <span>ERACLOUD</span>
    </div>
  );
}

export default function HomePage() {
  const [menuOpen, setMenuOpen] = useState(false);
  const [sent, setSent] = useState(false);
  const [errors, setErrors] = useState({});
  const [caseOpen, setCaseOpen] = useState(false);
  const [selectedPackage, setSelectedPackage] = useState('');
  const [form, setForm] = useState(initialForm);

  useEffect(() => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) return;
    const root = document.documentElement;
    let raf = 0;
    const onMove = (e) => {
      root.style.setProperty('--mx', `${e.clientX}px`);
      root.style.setProperty('--my', `${e.clientY}px`);
      root.style.setProperty('--sx', `${window.scrollY}px`);
    };
    const onScroll = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => root.style.setProperty('--sy', `${window.scrollY}px`));
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('scroll', onScroll);
      cancelAnimationFrame(raf);
    };
  }, []);

  const year = useMemo(() => new Date().getFullYear(), []);
  const scrollTo = (id) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });

  const choosePackage = (pkg) => {
    setSelectedPackage(pkg);
    setForm((prev) => ({ ...prev, service: pkg }));
    scrollTo('inquiry');
  };

  const submit = (e) => {
    e.preventDefault();
    const nextErrors = {};
    if (!form.name.trim()) nextErrors.name = 'Name is required.';
    if (!form.email.trim()) nextErrors.email = 'Email is required.';
    if (form.email && !/\S+@\S+\.\S+/.test(form.email)) nextErrors.email = 'Please use a valid email.';
    if (!form.service) nextErrors.service = 'Please select a service.';
    if (!form.message.trim()) nextErrors.message = 'Please add a project message.';
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) return;
    setSent(true);
    setTimeout(() => setSent(false), 5500);
  };

  return (<main>
    <div className="bg-layers" /><div className="cursor-glow" />
    <header className="header glass"><BrandLogo />
      <button className="menu-toggle" type="button" aria-expanded={menuOpen} onClick={() => setMenuOpen((v) => !v)}>☰</button>
      <nav className={`nav ${menuOpen ? 'open' : ''}`}>{navItems.map(([label, id]) => <button key={id} onClick={() => { scrollTo(id); setMenuOpen(false); }}>{label}</button>)}</nav>
      <button className="cta" onClick={() => scrollTo('inquiry')}>Start Project</button></header>

    <section className="hero section"><div className="hero-left"><span className="badge">Premium Digital Systems</span><h1>Automate.<br />Scale.<br /><span>Elevate.</span></h1><p>ERACLOUD builds premium websites, AI automations, branded visuals, and digital systems that help businesses look better, work faster, and attract more clients.</p><div className="hero-cta"><button className="cta" onClick={() => scrollTo('inquiry')}>Start Project</button><button className="ghost" onClick={() => scrollTo('services')}>View Services</button></div></div></section>

    <section className="feature-strip glass section">{['Premium Websites', 'AI Automation', 'Branded Visuals', 'Growth Systems'].map((f) => <article key={f}><span>✦</span><p>{f}</p></article>)}</section>
    <section id="services" className="section"><h2>Everything your business needs to look premium online.</h2><p className="sub">From website to automation — we build the digital foundation that helps your business move faster and attract better clients.</p><div className="grid">{services.map(([t, d]) => <article className="glass card luxe-card" key={t}><h3>{t}</h3><p>{d}</p></article>)}</div></section>
    <section id="process" className="section"><h2>From idea to launch — clear, fast, premium.</h2><div className="timeline">{processSteps.map(([t, d]) => <article className="glass step luxe-card" key={t}><h3>{t}</h3><p>{d}</p></article>)}</div></section>

    <section id="pricing" className="section aura"><h2>Flexible pricing for modern businesses.</h2><p className="sub">Every project is different. We adapt to your budget, speed, complexity, and business goals.</p><div className="prices">{[['Starter Launch', 'from 250€', 'Start with Starter'], ['Growth System', 'from 500€', 'Build Growth System', 'Most requested'], ['Premium Experience', 'up to 1000€', 'Create Premium Experience']].map(([n, p, cta, b]) => <article className={`glass price ${b ? 'featured' : ''}`} key={n}>{b && <small>{b}</small>}<h3>{n}</h3><h4>{p}</h4><button className={b ? 'cta' : 'ghost'} onClick={() => choosePackage(n)}>{cta}</button></article>)}</div></section>

    <section id="inquiry" className="section inquiry-grid aura"><div className="inquiry-copy"><span className="badge">Premium application panel</span><h2>Apply for your digital system.</h2><p>Tell us what you want to build. We’ll review your request and show you the best next step for your business.</p>{selectedPackage && <p className="selected">Selected package: {selectedPackage}</p>}</div>
      <form className="glass inquiry-form" onSubmit={submit} noValidate>
        <label><span>Name*</span><input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />{errors.name && <em>{errors.name}</em>}</label>
        <label><span>Business name</span><input value={form.business} onChange={(e) => setForm({ ...form, business: e.target.value })} /></label>
        <label><span>Email*</span><input value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} />{errors.email && <em>{errors.email}</em>}</label>
        <label><span>Phone / WhatsApp</span><input value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} /></label>
        <label><span>Website or Instagram link</span><input value={form.site} onChange={(e) => setForm({ ...form, site: e.target.value })} /></label>
        <label><span>Service needed*</span><select value={form.service} onChange={(e) => setForm({ ...form, service: e.target.value })}><option value="">Select a service</option><option>Starter Launch</option><option>Growth System</option><option>Premium Experience</option><option>Website</option><option>AI Automation</option></select>{errors.service && <em>{errors.service}</em>}</label>
        <label><span>Budget</span><select value={form.budget} onChange={(e) => setForm({ ...form, budget: e.target.value })}><option value="">Select budget</option><option>250–500€</option><option>500–750€</option><option>750–1000€</option></select></label>
        <label className="full"><span>Project message*</span><textarea rows={5} value={form.message} onChange={(e) => setForm({ ...form, message: e.target.value })} />{errors.message && <em>{errors.message}</em>}</label>
        <button className="cta submit">Send Project Request</button><p className="trust">No pressure. Just send your idea — we’ll respond with a clear next step.</p>{sent && <p className="success">Thank you. Your request was prepared successfully.</p>}
      </form></section>

    <section id="projects" className="section"><h2>Projects coming soon.</h2><p className="sub">Our first public transformations and case studies will be added soon.</p><div className="grid projects-grid">{['Website transformations', 'AI automation systems', 'Brand visuals & growth'].map((item) => <button className="glass project-card" key={item} onClick={() => setCaseOpen(true)}><span className="coming">Coming soon</span><div className="preview" /><h3>{item}</h3><p>Locked preview</p></button>)}</div></section>

    <section className="section" id="contact-cards"><h2>Let’s build your digital system.</h2><p className="sub">Send us your idea — we’ll show you how it can look, work, and convert better.</p><div className="contact-grid">{contactCards.map(([type, l, v, d, href, cta]) => <article key={v} className="glass contact-card"><div className="label-row"><span>{type === 'email' ? '✉' : type === 'phone' ? '☎' : '📍'}</span><b>{l}</b></div><h3>{v}</h3><p>{d}</p><a href={href} target={type === 'location' ? '_blank' : undefined} rel="noreferrer">{cta} ↗</a></article>)}</div></section>

    <footer className="section footer glass"><h3>ERACLOUD</h3><nav>{navItems.map(([label, id]) => <button key={id} onClick={() => scrollTo(id)}>{label}</button>)}</nav><small>Premium digital systems for modern businesses. © {year}</small></footer>

    {caseOpen && <div className="modal-backdrop" onClick={() => setCaseOpen(false)}><div className="modal glass" onClick={(e) => e.stopPropagation()}><h3>Case studies are being prepared and will be published soon.</h3><button className="cta" onClick={() => setCaseOpen(false)}>Close</button></div></div>}
  </main>);
}
