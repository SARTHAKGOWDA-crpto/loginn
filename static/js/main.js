/* NEXUS AUTH — main.js */

// Auto-dismiss toasts after 5 seconds
document.addEventListener('DOMContentLoaded', () => {
  const toasts = document.querySelectorAll('.toast');
  toasts.forEach((toast, i) => {
    setTimeout(() => {
      toast.style.transition = 'opacity 0.4s, transform 0.4s';
      toast.style.opacity = '0';
      toast.style.transform = 'translateX(20px)';
      setTimeout(() => toast.remove(), 400);
    }, 4000 + i * 300);
  });
});
