export function showNotification(message, type = 'info') {
    const container = document.getElementById('event-notification');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `notification ${type}`;

    const icons = {
        success: '✅',
        error: '❌',
        warning: '⚠️',
        info: 'ℹ️'
    };

    toast.innerHTML = `
        <span class="notification-icon">${icons[type] || icons.info}</span>
        <span class="notification-text" style="flex:1; color:#333;">${message}</span>
        <span class="notification-close" style="cursor:pointer; opacity:0.5;">&times;</span>
    `;

    container.appendChild(toast);

    // Удаление
    const remove = () => {
        toast.classList.add('fade-out');
        setTimeout(() => toast.remove(), 500);
    };

    toast.querySelector('.notification-close').onclick = remove;
    setTimeout(remove, 4000);
}