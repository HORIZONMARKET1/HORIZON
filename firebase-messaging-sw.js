/* HORIZON MARKET — Service Worker
   Офлайн-кэш: сама страница (app shell) + шрифты/иконки/Firebase SDK,
   чтобы приложение открывалось и грид не был пустым без интернета.

   Данные каталога (товары, заказы и т.д.) кэшируются НЕ здесь, а через
   Firestore offline persistence (IndexedDB) — это надёжнее для "живых" данных.
   Service worker отвечает только за саму страницу и статику.

   Push-уведомления обслуживает отдельный service worker OneSignal
   (OneSignalSDKWorker.js, регистрируется автоматически SDK-скриптом OneSignal,
   см. вкладку "Push" в админ-панели) — он не связан с этим файлом. */

const CACHE_VERSION = 'v1';
const SHELL_CACHE = 'hz-shell-' + CACHE_VERSION;

// Хосты, статику с которых можно безопасно кэшировать и отдавать офлайн
const CACHEABLE_HOSTS = [
  self.location.hostname,       // сама страница, PNG-иконки и т.д.
  'fonts.googleapis.com',
  'fonts.gstatic.com',
  'cdnjs.cloudflare.com',       // Font Awesome
  'www.gstatic.com'             // Firebase SDK
];

// Хосты/пути, которые НИКОГДА нельзя кэшировать (живые данные, авторизация)
function isNeverCache(url){
  return url.hostname.includes('firestore.googleapis.com')
      || url.hostname.includes('googleapis.com') && url.pathname.includes('/google.firestore')
      || url.hostname.includes('firebaseinstallations.googleapis.com')
      || url.hostname.includes('fcmregistrations.googleapis.com')
      || url.hostname.includes('identitytoolkit.googleapis.com')
      || url.hostname.includes('securetoken.googleapis.com');
}

self.addEventListener('install', (event)=>{
  self.skipWaiting();
  event.waitUntil(
    caches.open(SHELL_CACHE).then(cache=>{
      // Кэшируем саму страницу сразу при установке, чтобы офлайн-открытие работало
      // даже если человек ни разу не заходил после установки SW.
      return cache.addAll(['./', './index.html']).catch(()=>{ /* один из путей может не существовать — не критично */ });
    })
  );
});

self.addEventListener('activate', (event)=>{
  event.waitUntil(
    Promise.all([
      // Удаляем кэши от старых версий SW
      caches.keys().then(keys=>Promise.all(
        keys.filter(k=>k.startsWith('hz-shell-') && k!==SHELL_CACHE).map(k=>caches.delete(k))
      )),
      self.clients.claim()
    ])
  );
});

self.addEventListener('fetch', (event)=>{
  const req = event.request;
  if(req.method !== 'GET') return; // не трогаем POST/PUT и т.п.

  const url = new URL(req.url);
  if(isNeverCache(url)) return; // живые данные/авторизация — всегда напрямую в сеть

  // Навигация (открытие/обновление страницы) — сеть в приоритете, кэш как офлайн-фоллбек
  if(req.mode === 'navigate'){
    event.respondWith(
      fetch(req).then(res=>{
        const copy = res.clone();
        caches.open(SHELL_CACHE).then(cache=>cache.put('./index.html', copy));
        return res;
      }).catch(()=>caches.match('./index.html').then(r=>r || caches.match('./')))
    );
    return;
  }

  if(!CACHEABLE_HOSTS.includes(url.hostname)) return; // прочие сторонние хосты не трогаем

  // Статика (шрифты, иконки, Firebase SDK и т.д.) — кэш в приоритете + фоновое обновление
  event.respondWith(
    caches.open(SHELL_CACHE).then(cache=>
      cache.match(req).then(cached=>{
        const network = fetch(req).then(res=>{
          if(res && (res.status === 200 || res.type === 'opaque')) cache.put(req, res.clone());
          return res;
        }).catch(()=>cached);
        return cached || network;
      })
    )
  );
});

