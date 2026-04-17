// Applies the saved theme ASAP (before paint) and wires up the theme toggle button.
// Theme is stored in localStorage as: theme = 'dark' | 'light'.
(function(){
	const STORAGE_KEY = 'nexus_theme_preference';

	function getStored(){
		try { return localStorage.getItem(STORAGE_KEY); } catch { return null; }
	}

	function apply(mode){
		const m = (mode === 'light') ? 'light' : 'dark';
		try{
			document.documentElement.setAttribute('data-theme', m);
			// Also update icons/labels in the specific navbar structure
			const sun = document.querySelector('.theme-toggle .sun');
			const moon = document.querySelector('.theme-toggle .moon');
			const label = document.querySelector('.theme-toggle .theme-label');
			if (sun && moon) {
				sun.style.display = (m === 'dark') ? 'block' : 'none';
				moon.style.display = (m === 'dark') ? 'none' : 'block';
			}
			if (label) {
				label.textContent = (m === 'dark') ? 'Dark' : 'Light';
			}
		}catch{}
	}

	// Initial apply (ASAP)
	const saved = getStored();
	apply(saved || 'dark');

	// After DOM is ready, sync toggle if present
	document.addEventListener('DOMContentLoaded', function(){
		const btn = document.getElementById('themeToggle');
		if (!btn) return;
		
		btn.addEventListener('click', function(e){
			e.preventDefault();
			const curr = document.documentElement.getAttribute('data-theme') || 'dark';
			const next = (curr === 'light') ? 'dark' : 'light';
			try { localStorage.setItem(STORAGE_KEY, next); } catch {}
			apply(next);
		});
	});
})();
