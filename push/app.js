const VAPID_PUBLIC_KEY =
  "BPxnijQP_ftCrmmaRYffDZ3HEQYADTsQBOrAeR_z1Y3UgB97u_1W5lRC6VVDyHZD8XkyBANg-zmV9XDriMNNpCE";

function urlBase64ToUint8Array(base64String) {
  const padding = "=".repeat((4 - (base64String.length % 4)) % 4);
  const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
  const rawData = atob(base64);
  return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
}

const statusEl = document.getElementById("status");
const outputEl = document.getElementById("output");
const enableBtn = document.getElementById("enable-btn");
const copyBtn = document.getElementById("copy-btn");

function setStatus(text) {
  statusEl.textContent = text;
}

async function enablePush() {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
    setStatus("이 브라우저는 웹푸시를 지원하지 않습니다. 사파리로 '홈 화면에 추가' 후 다시 시도해주세요.");
    return;
  }

  const permission = await Notification.requestPermission();
  if (permission !== "granted") {
    setStatus("알림 권한이 거부되었습니다. 설정에서 알림을 허용해주세요.");
    return;
  }

  setStatus("등록 중...");
  const registration = await navigator.serviceWorker.register("./sw.js");
  await navigator.serviceWorker.ready;

  let subscription = await registration.pushManager.getSubscription();
  if (!subscription) {
    subscription = await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC_KEY),
    });
  }

  const json = JSON.stringify(subscription.toJSON(), null, 2);
  outputEl.textContent = json;
  outputEl.style.display = "block";
  copyBtn.style.display = "inline-block";
  setStatus("알림이 켜졌습니다. 아래 값을 복사해서 GitHub Secret(PUSH_SUBSCRIPTION)에 붙여넣어주세요.");
}

enableBtn.addEventListener("click", enablePush);

copyBtn.addEventListener("click", async () => {
  await navigator.clipboard.writeText(outputEl.textContent);
  copyBtn.textContent = "복사됨!";
  setTimeout(() => (copyBtn.textContent = "값 복사하기"), 1500);
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("./sw.js").catch(() => {});
}
