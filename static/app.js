const kind = document.querySelector('#kind');
if (kind) {
  const update = () => {
    const file = kind.value === 'file';
    document.querySelector('#file-field').hidden = !file;
    document.querySelector('#content-field').hidden = file;
    document.querySelector('[name=file]').required = file;
    document.querySelector('[name=content]').required = !file;
    document.querySelector('#content-label').textContent = kind.value === 'link' ? 'URL (https://…) ' : 'Texto';
  };
  kind.addEventListener('change', update);
  update();
}
document.querySelectorAll('.copy').forEach(button => button.addEventListener('click', async () => {
  const url = new URL(button.dataset.path, location.origin).href;
  try { await navigator.clipboard.writeText(url); document.querySelector('#notice').textContent = 'Enlace copiado. Ya puedes compartirlo.'; }
  catch { window.prompt('Copia este enlace:', url); }
}));
setInterval(() => document.querySelectorAll('[data-expires]').forEach(node => {
  const seconds = Number(node.dataset.expires) - Date.now() / 1000;
  node.textContent = seconds > 0 ? `Caduca en ${Math.ceil(seconds / 60)} min` : 'Caducado';
  if (seconds <= 0) node.closest('article').remove();
}), 1000);
