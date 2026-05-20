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
    const move = (e) => {
      tx = e.clientX;
      ty = e.clientY;
    };
    const loop = () => {
      x += (tx - x) * 0.08;
      y += (ty - y) * 0.08;
      root.style.setProperty('--mx', `${x}px`);
      root.style.setProperty('--my', `${y}px`);
      requestAnimationFrame(loop);
    };
    window.addEventListener('pointermove', move);
    loop();
    return () => window.removeEventListener('pointermove', move);
  }, []);

  const year = useMemo(() => new Date().getFullYear(), []);

  return (
    <main>
      <div className="cursor-glow" />
      <header className="header glass">
        <div className="logo">ERACLOUD</div>
        <nav className={`nav ${menuOpen ? 'open' : ''}`}>
          {navItems.map((item) => <a key={item} href={`#${item.toLowerCase()}`}>{item}</a>)}
        </nav>
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

      <section className="feature-strip glass section">
        {['Premium Websites','AI Automation','Branded Visuals','Growth Systems'].map((f)=><article key={f}><span>✦</span><p>{f}</p></article>)}
      </section>

      <section id="services" className="section">
        <h2>Everything your business needs to look premium online.</h2><p className="sub">From website to automation — we build the digital foundation that helps your business move faster and attract better clients.</p>
        <div className="grid">{services.map(([t,d])=><article className="glass card" key={t}><h3>{t}</h3><p>{d}</p><span>→</span></article>)}</div>
      </section>

      <section id="process" className="section">
        <h2>From idea to launch — clear, fast, premium.</h2>
        <div className="timeline">{processSteps.map(([t,d])=><article className="glass step" key={t}><h3>{t}</h3><p>{d}</p></article>)}</div>
      </section>

      <section id="pricing" className="section">
        <h2>Flexible pricing for modern businesses.</h2><p className="sub">Every project is different. We adapt to your budget, speed, complexity, and business goals.</p>
        <div className="prices">
          {[
            ['Starter Launch','from 250€','For small businesses that need a clean online foundation.',['one-page landing page','basic branding polish','contact CTA','mobile responsive structure','social media visual foundation'],'Start with Starter'],
            ['Growth System','from 500€','For businesses that need a stronger digital presence and better conversion flow.',['premium website structure','social media setup','branded visuals','lead/contact funnel','basic SEO setup','Meta Ads setup guidance'],'Build Growth System','Most requested'],
            ['Premium Experience','up to 1000€','For businesses that want a complete premium digital system.',['high-end website','AI automation concept/setup','branded visuals','social media foundation','contact/request system','conversion-focused structure','launch support'],'Create Premium Experience']
          ].map(([n,p,d,li,c,b])=><article className={`glass price ${b ? 'featured':''}`} key={n}><h3>{n}</h3>{b&&<small>{b}</small>}<h4>{p}</h4><p>{d}</p><ul>{li.map(i=><li key={i}>{i}</li>)}</ul><button className="ghost">{c}</button></article>)}
        </div>
        <p className="note">Final price depends on speed, complexity, content, and project scope.</p>
      </section>

      <section id="contact" className="section form-wrap">
        <h2>Tell us about your project.</h2><p className="sub">Send your idea, business, or current online presence — we’ll show you how it can look, work, and convert better.</p>
        <form className="glass form" onSubmit={(e)=>{e.preventDefault();setSent(true);}}>{['Name','Business name','Email','Phone / WhatsApp','Website or Instagram link'].map((f)=><input placeholder={f} key={f} required={f==='Name'||f==='Email'} />)}
          <select><option>What do you need?</option><option>Website</option><option>AI Automation</option><option>Branding / Visuals</option><option>Social Media Setup</option><option>Meta Ads</option><option>Full Digital System</option></select>
          <select><option>Budget</option><option>250–500€</option><option>500–750€</option><option>750–1000€</option></select>
          <textarea placeholder="Project message" rows={5} />
          <button className="cta">Send Request</button>{sent && <p className="success">Request sent. We will contact you shortly.</p>}
        </form>
      </section>

      <section id="projects" className="section"><h2>Projects coming soon.</h2><p className="sub">Our first public transformations and case studies will be added here soon.</p><div className="grid"><article className="glass card blur">Before/After transformations</article><article className="glass card blur">Client websites</article><article className="glass card blur">Branded visuals + results</article></div></section>

      <section className="section contact"><h2>Let’s build your digital system.</h2><p>Send us your idea — we’ll show you how it can look, work, and convert better.</p><p>era.cloud.co@gmail.com · +49 160 91408872 · +49 151 53111186 · Kempten, Germany</p><div className="hero-cta"><button className="cta">Contact ERACLOUD</button><button className="ghost">Instagram</button><button className="ghost">Email</button></div></section>

      <footer className="section footer glass"><h3>ERACLOUD</h3><p>Automate. Scale. Elevate.</p><nav>{navItems.map((item) => <a key={item} href={`#${item.toLowerCase()}`}>{item}</a>)}</nav><small>Premium digital systems for modern businesses. © {year}</small></footer>
    </main>
  );
}
