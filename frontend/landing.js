// Nav blur/shadow on scroll
const nav = document.getElementById("nav");
window.addEventListener("scroll", () => {
  nav.classList.toggle("scrolled", window.scrollY > 8);
}, { passive: true });

// Scroll-reveal for elements with .reveal
const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add("in-view");
      revealObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.15, rootMargin: "0px 0px -40px 0px" });

document.querySelectorAll(".reveal").forEach(el => revealObserver.observe(el));

// Animated count-up for stats, triggered once when in view
function animateCount(el) {
  const target = Number(el.dataset.target);
  const suffix = el.dataset.suffix || "";
  const duration = 1400;
  const start = performance.now();

  function tick(now) {
    const progress = Math.min((now - start) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
    const value = Math.round(target * eased);
    el.textContent = value.toLocaleString("en-IN") + suffix;
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

const statObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      animateCount(entry.target);
      statObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.4 });

document.querySelectorAll(".stat-num").forEach(el => statObserver.observe(el));

// Accordion (how it works)
const accordionItems = document.querySelectorAll(".accordion-item");
const accordionPanels = document.querySelectorAll(".accordion-preview-panel");

function setAccordionStep(step) {
  accordionItems.forEach(item => item.classList.toggle("active", item.dataset.step === step));
  accordionPanels.forEach(panel => panel.classList.toggle("active", panel.dataset.panel === step));
}
accordionItems.forEach(item => {
  item.addEventListener("click", () => setAccordionStep(item.dataset.step));
});
setAccordionStep("1"); // ensure panel 1 is visible on load

// Chat bubble: swap typing dots for the reply once the card scrolls into view
const chatBubble = document.getElementById("chatBotBubble");
if (chatBubble) {
  const chatObserver = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        setTimeout(() => {
          chatBubble.innerHTML = "Matched: <strong>Widow Pension Scheme</strong> — 92% confidence";
        }, 1300);
        chatObserver.unobserve(entry.target);
      }
    });
  }, { threshold: 0.5 });
  chatObserver.observe(chatBubble);
}
