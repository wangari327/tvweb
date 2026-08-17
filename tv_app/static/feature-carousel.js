document.addEventListener('DOMContentLoaded', () => {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  document.querySelectorAll('[data-feature-carousel]').forEach((carousel) => {
    const slides = [...carousel.querySelectorAll('[data-feature-slide]')];
    const dots = [...carousel.querySelectorAll('[data-feature-dot]')];
    if (slides.length < 2) return;

    const interval = Number(carousel.dataset.interval) || 6500;
    let active = slides.findIndex((slide) => slide.classList.contains('is-active'));
    let timer;

    const show = (next) => {
      active = (next + slides.length) % slides.length;
      slides.forEach((slide, index) => {
        const selected = index === active;
        slide.classList.toggle('is-active', selected);
        slide.setAttribute('aria-hidden', String(!selected));
      });
      dots.forEach((dot, index) => {
        const selected = index === active;
        dot.classList.toggle('is-active', selected);
        dot.setAttribute('aria-pressed', String(selected));
      });
    };

    const start = () => {
      window.clearInterval(timer);
      timer = window.setInterval(() => show(active + 1), interval);
    };

    dots.forEach((dot) => dot.addEventListener('click', () => {
      show(Number(dot.dataset.featureIndex));
      start();
    }));
    carousel.addEventListener('mouseenter', () => window.clearInterval(timer));
    carousel.addEventListener('mouseleave', start);
    carousel.addEventListener('focusin', () => window.clearInterval(timer));
    carousel.addEventListener('focusout', (event) => {
      if (!carousel.contains(event.relatedTarget)) start();
    });
    start();
  });
});
