"use client";
/**
 * src/app/(landing)/page.tsx
 * ----------------------------
 * Earned Autonomy Engine — Landing Page (Enhanced)
 *
 * Full-viewport, scroll-driven presentation page inspired by sentientx.com.
 * Content sourced from the 10-minute demo presentation deck.
 * Features: sticky nav with Deloitte logo, floating particles, scroll-triggered
 * animations, gradient glows, hover effects, and staggered reveals.
 */

import { useEffect, useRef, useCallback, useState } from "react";
import Link from "next/link";
import "./landing.css";

/* ============================================================
   INTERSECTION OBSERVER HOOK
   ============================================================ */

function useScrollReveal() {
  const containerRef = useRef<HTMLDivElement>(null);

  const observe = useCallback(() => {
    if (!containerRef.current) return;
    const elements = containerRef.current.querySelectorAll(
      ".fade-up, .fade-in, .scale-in, .slide-left, .slide-right"
    );

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            entry.target.classList.add("in-view");
          }
        });
      },
      { threshold: 0.1, rootMargin: "0px 0px -30px 0px" }
    );

    elements.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const cleanup = observe();
    return cleanup;
  }, [observe]);

  return containerRef;
}

/* ============================================================
   NAV SCROLL HOOK
   ============================================================ */

function useNavScroll() {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const handler = () => setScrolled(window.scrollY > 60);
    window.addEventListener("scroll", handler, { passive: true });
    return () => window.removeEventListener("scroll", handler);
  }, []);

  return scrolled;
}

/* ============================================================
   FLOATING PARTICLES COMPONENT
   ============================================================ */

function Particles() {
  return (
    <div className="particles">
      <div className="particle" />
      <div className="particle" />
      <div className="particle" />
      <div className="particle" />
      <div className="particle" />
      <div className="particle" />
    </div>
  );
}

/* ============================================================
   LANDING PAGE
   ============================================================ */

export default function LandingPage() {
  const ref = useScrollReveal();
  const navScrolled = useNavScroll();

  return (
    <div ref={ref} className="landing-page">
      {/* ──────────────────────────────────────────────────────────
          STICKY NAVIGATION
          ────────────────────────────────────────────────────────── */}
      <nav className={`landing-nav ${navScrolled ? "nav-scrolled" : ""}`}>
        <div style={{ display: "flex", alignItems: "center" }}>
          <Link href="/" className="nav-brand">
            <span className="nav-brand-name">Deloitte</span>
            <span className="nav-brand-dot" />
          </Link>
          <span className="nav-product">Earned Autonomy Engine</span>
        </div>

        <div className="nav-links">
          <a href="#concept" className="nav-link">Concept</a>
          <a href="#architecture" className="nav-link">Architecture</a>
          <a href="#insight" className="nav-link">Insight</a>
          <a href="#numbers" className="nav-link">Numbers</a>
          <Link href="/agents" className="nav-link nav-link--cta">
            Dashboard →
          </Link>
        </div>
      </nav>

      {/* ──────────────────────────────────────────────────────────
          SECTION 1: HERO
          ────────────────────────────────────────────────────────── */}
      <section className="landing-section landing-section--hero landing-dark">
        {/* Animated gradient glows */}
        <div className="hero-glow hero-glow--1" />
        <div className="hero-glow hero-glow--2" />
        <Particles />

        <div className="landing-container" style={{ textAlign: "center", position: "relative", zIndex: 1 }}>
          <span className="landing-eyebrow fade-up">
            Deloitte AI Governance
          </span>

          <h1 className="landing-h1 fade-up" style={{ maxWidth: 800, margin: "0 auto 1.5rem" }}>
            Would you let AI approve a{" "}
            <span className="text-green-accent">₹10,000</span> invoice on day
            one?
            <span className="hero-cursor" />
          </h1>

          <p className="hero-subtitle fade-up">
            Companies want AI agents to act, not just advise. Two bad options
            today: full autonomy with no safety, or approve everything with no
            benefit. Nobody can answer the actual question —{" "}
            <em className="text-emphasis">when has it earned more?</em>
          </p>

          <div className="fade-up" style={{ marginTop: "1rem" }}>
            <Link href="/agents" className="landing-cta">
              Enter Dashboard
              <span className="landing-cta-arrow">→</span>
            </Link>
          </div>
        </div>

        
      </section>

      {/* ──────────────────────────────────────────────────────────
          SECTION 2: THE CONCEPT
          ────────────────────────────────────────────────────────── */}
      <section id="concept" className="landing-section landing-light">
        <div className="landing-container">
          <span className="landing-eyebrow fade-up">The Concept</span>

          <h2 className="landing-h2 fade-up">
            Autonomy as something earned,{" "}
            <span style={{ color: "#86BC25" }}>on evidence.</span>
          </h2>

          <p className="landing-body fade-up">
            An AI approves invoices. It starts at <strong>₹500</strong> alone —
            anything larger, it escalates. A statistics engine watches it.
            Enough proof, and the system recommends a rise up a five-rung
            ladder. Four AI governance agents write the case.{" "}
            <strong>A human approves every increase.</strong>
          </p>

          <div className="section-line fade-in" />

          {/* Five-Rung Ladder Visual */}
          <div className="ladder-container fade-up">
            {[
              { rung: 0, amount: "₹500", label: "Floor" },
              { rung: 1, amount: "₹1,000", label: "Rung 1" },
              { rung: 2, amount: "₹2,500", label: "Rung 2" },
              { rung: 3, amount: "₹5,000", label: "Rung 3" },
              { rung: 4, amount: "₹10,000", label: "Ceiling" },
            ].map((step) => (
              <div key={step.rung} className="ladder-rung">
                <div className="ladder-amount">{step.amount}</div>
                <div className={`ladder-bar ladder-bar--${step.rung}`} />
                <span className="ladder-label" style={{ color: "#64748B" }}>
                  {step.label}
                </span>
              </div>
            ))}
          </div>

          <div className="landing-quote slide-left">
            The AI reasons. Statistics provide evidence. The Policy Engine
            enforces. Humans authorise increases — nobody has to authorise a
            reduction.
          </div>

          <p className="landing-body fade-up" style={{ marginTop: "1.5rem" }}>
            Performance drops → authority is taken back{" "}
            <strong>automatically, no human needed.</strong>
          </p>
        </div>
      </section>

      {/* ──────────────────────────────────────────────────────────
          SECTION 3: ARCHITECTURE
          ────────────────────────────────────────────────────────── */}
      <section id="architecture" className="landing-section landing-dark">
        <Particles />
        <div className="landing-container">
          <span className="landing-eyebrow fade-up">Architecture</span>

          <h2 className="landing-h2 fade-up">
            Four layers, one contract.
          </h2>

          <div className="arch-flow stagger">
            <div className="arch-node fade-up">
              <div className="arch-node-label">Frontend</div>
              <div className="arch-node-name">Dashboard</div>
              <div className="arch-node-tech">Next.js · :3000</div>
            </div>

            <div className="arch-node fade-up">
              <div className="arch-node-label">Backend</div>
              <div className="arch-node-name">API &amp; Persistence</div>
              <div className="arch-node-tech">FastAPI · PostgreSQL</div>
            </div>

            <div className="arch-node arch-node--accent fade-up">
              <div className="arch-node-label">Trust Engine</div>
              <div className="arch-node-name">Pure Python</div>
              <div className="arch-node-tech">
                No libraries · Every formula defensible
              </div>
            </div>

            <div className="arch-node fade-up">
              <div className="arch-node-label">Governance</div>
              <div className="arch-node-name">4 AI Agents</div>
              <div className="arch-node-tech">
                Independent panel · Cached responses
              </div>
            </div>
          </div>

          <div className="section-line fade-in" />

          <div className="fade-up" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1.5rem", maxWidth: 700, marginTop: "1rem" }}>
            <div className="callout-box">
              <p className="landing-caption" style={{ color: "#94A3B8" }}>
                <span style={{ color: "#86BC25", fontWeight: 600 }}>The trust engine imports nothing</span>{" "}
                — pure Python, no libraries. Every formula is defensible
                line by line, and the numbers reproduce anywhere.
              </p>
            </div>
            <div className="callout-box">
              <p className="landing-caption" style={{ color: "#94A3B8" }}>
                <span style={{ color: "#86BC25", fontWeight: 600 }}>One shared contracts package</span>{" "}
                — all four layers import it, so four people building in
                parallel couldn&apos;t drift apart.
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ──────────────────────────────────────────────────────────
          SECTION 4: THE KEY INSIGHT — "100% accurate. Refused."
          This is the strongest section.
          ────────────────────────────────────────────────────────── */}
      <section id="insight" className="landing-section landing-light">
        <div className="landing-container">
          <span className="landing-eyebrow fade-up">The Key Insight</span>

          <h2 className="landing-h2 fade-up">
            100% accurate.{" "}
            <span style={{ color: "#86BC25" }}>Refused.</span>
          </h2>

          <div className="insight-grid">
            <div className="slide-left">
              <div className="landing-table-wrapper scale-in">
                <table className="landing-table">
                  <thead>
                    <tr>
                      <th>Decisions</th>
                      <th>Observed</th>
                      <th>Proven (Wilson)</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td>22</td>
                      <td>100%</td>
                      <td className="td-highlight">85.1%</td>
                    </tr>
                    <tr>
                      <td>44</td>
                      <td>100%</td>
                      <td className="td-highlight">92.0%</td>
                    </tr>
                    <tr>
                      <td>87</td>
                      <td>98.9%</td>
                      <td className="td-highlight">93.8%</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </div>

            <div className="slide-right">
              <p className="landing-body">
                Twenty-two out of twenty-two correct — and the system refused to
                give it more authority. On twenty-two samples a perfect record
                only <em>proves</em> 85%.
              </p>
              <p className="landing-body" style={{ marginTop: "1rem" }}>
                <strong>We never use the observed number.</strong> We use the
                number we can prove.
              </p>
              <div className="section-line fade-in" />
              <p className="landing-body fade-up">
                As evidence accumulates, that gap closes.{" "}
                <strong style={{ color: "#86BC25" }}>
                  The narrowing is what earns the next rung.
                </strong>
              </p>
            </div>
          </div>
        </div>
      </section>

      {/* ──────────────────────────────────────────────────────────
          SECTION 5: THE FOUR-COMPONENT SCORE
          ────────────────────────────────────────────────────────── */}
      <section className="landing-section landing-dark">
        <Particles />
        <div className="landing-container">
          <span className="landing-eyebrow fade-up">Trust Score</span>

          <h2 className="landing-h2 fade-up">
            Four components, weighted by what matters.
          </h2>

          <div className="score-grid stagger">
            {[
              {
                weight: "0.50",
                name: "Accuracy",
                desc: "Wilson lower bound — not the point estimate, the proven floor.",
              },
              {
                weight: "0.25",
                name: "Human Agreement",
                desc: "How often the agent's escalated decisions match human rulings.",
              },
              {
                weight: "0.15",
                name: "Critical-Error Penalty",
                desc: "Approvals that should have been rejections. Real money out the door.",
              },
              {
                weight: "0.10",
                name: "Autonomy Utilisation",
                desc: "Is the agent actually using its authority, or escalating everything?",
              },
            ].map((c) => (
              <div key={c.name} className="score-card fade-up">
                <div className="score-card-weight">{c.weight}</div>
                <div className="score-card-name">{c.name}</div>
                <div className="score-card-desc">{c.desc}</div>
              </div>
            ))}
          </div>

          <div className="section-line fade-in" />

          <p className="landing-body fade-up" style={{ color: "#64748B", marginTop: "0.5rem" }}>
            If a component has no evidence, we don&apos;t guess it — we drop it,
            redistribute its weight, and the evaluation says so in its reason
            codes.
          </p>
        </div>
      </section>

      {/* ──────────────────────────────────────────────────────────
          SECTION 6: THE NUMBERS
          ────────────────────────────────────────────────────────── */}
      <section id="numbers" className="landing-section landing-light">
        <div className="landing-container">
          <span className="landing-eyebrow fade-up" style={{ textAlign: "center", display: "block" }}>
            The Numbers
          </span>

          <h2 className="landing-h2 fade-up" style={{ textAlign: "center" }}>
            One simulation run. Zero lost.
          </h2>

          <div className="stat-strip stagger">
            <div className="stat-item fade-up">
              <div className="stat-number stat-number--green">1,550</div>
              <div className="stat-label">
                decisions in one run, zero lost
              </div>
            </div>
            <div className="stat-item fade-up">
              <div className="stat-number" style={{ color: "#0F172A" }}>
                Byte-for-byte
              </div>
              <div className="stat-label">
                same seed, identical run — every time
              </div>
            </div>
            <div className="stat-item fade-up">
              <div className="stat-number stat-number--green">
                100%→5%
              </div>
              <div className="stat-label">
                review burden from floor rung to top
              </div>
            </div>
          </div>

          <div className="section-line fade-in" />

          <div className="landing-quote fade-up" style={{ textAlign: "center", borderLeft: "none", padding: 0 }}>
            The payoff isn&apos;t just a bigger limit. At the floor, a human
            reviews every decision. At the top rung, five percent.{" "}
            <strong style={{ color: "#86BC25" }}>
              Earning autonomy earns less oversight
            </strong>{" "}
            — and that&apos;s the actual return on trusting this agent.
          </div>
        </div>
      </section>

      {/* ──────────────────────────────────────────────────────────
          SECTION 7: CTA / CLOSE
          ────────────────────────────────────────────────────────── */}
      <section className="landing-section landing-dark" style={{ textAlign: "center", paddingTop: "6rem", paddingBottom: "6rem" }}>
        <Particles />
        <div className="landing-container" style={{ position: "relative", zIndex: 1 }}>
          <span className="landing-eyebrow fade-up">
            Earned Autonomy Engine
          </span>

          <h2 className="landing-h2 fade-up" style={{ maxWidth: 700, margin: "0 auto 1.5rem" }}>
            Earned autonomy, and{" "}
            <span className="text-green-accent">earned oversight.</span>
          </h2>

          <p className="landing-body landing-body--centered fade-up" style={{ marginBottom: "2.5rem" }}>
            The AI reasons. Statistics provide evidence. The Policy Engine
            enforces. Humans authorise increases — and nobody has to authorise
            a reduction.
          </p>

          <div className="fade-up" style={{ display: "flex", gap: "1rem", justifyContent: "center", flexWrap: "wrap" }}>
            <Link href="/agents" className="landing-cta">
              Enter Dashboard
              <span className="landing-cta-arrow">→</span>
            </Link>
          </div>

          {/* Footer line */}
          <div className="section-line fade-in" style={{ marginTop: "3rem" }} />
          <p className="fade-in" style={{
            marginTop: "1.5rem",
            fontFamily: "var(--font-inter), 'Inter', sans-serif",
            fontSize: "0.75rem",
            color: "#475569",
            letterSpacing: "0.05em",
          }}>
            Deloitte AI Governance Platform · Earned Autonomy Engine
          </p>
        </div>
      </section>
    </div>
  );
}
