// firebase-messaging-sw.js
// ВАЖНО: этот файл должен лежать в КОРНЕ сайта (рядом с index.html),
// по адресу https://ваш-домен/firebase-messaging-sw.js — иначе браузер его не найдёт.
//
// Он отвечает за показ системного push-уведомления, когда сайт/приложение
// полностью закрыты (нет открытой вкладки и браузер тоже закрыт — на телефоне
// уведомление всё равно придёт, пока есть интернет, т.к. его показывает ОС,
// а не страница).

importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/9.23.0/firebase-messaging-compat.js');

// Эти значения должны СОВПАДАТЬ с конфигом Firebase, который вписан в index.html
// (window.HZ_EMBEDDED_CONFIG или сохранённый через админ-форму настройки).
// Если меняете проект Firebase — обновите значения и здесь.
firebase.initializeApp({
  apiKey: "AIzaSyBS4crsvCIRwYIvUQWSItcG5geNdU1NLq4",
  authDomain: "horizon-bdbf0.firebaseapp.com",
  projectId: "horizon-bdbf0",
  storageBucket: "horizon-bdbf0.firebasestorage.app",
  messagingSenderId: "983614809094",
  appId: "1:983614809094:web:531322b11099d85fd94f87"
});

const messaging = firebase.messaging();

// Показ уведомления, когда сайт закрыт / вкладка не активна
messaging.onBackgroundMessage((payload) => {
  const data = payload.notification || {};
  const title = data.title || 'HORIZON MARKET';
  const options = {
    body: data.body || '',
    icon: data.icon || 'https://raw.githubusercontent.com/gauravghongde/logos/main/logo192.png',
    badge: data.icon || undefined,
    data: payload.data || {},
    tag: (payload.data && payload.data.tag) || undefined
  };
  self.registration.showNotification(title, options);
});

// Клик по уведомлению — открыть/сфокусировать сайт (при необходимости на нужной странице)
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.click_action) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
