const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto('https://mp.weixin.qq.com/', { waitUntil: 'domcontentloaded' });

  console.log('请扫码登录公众号后台。登录成功后回到终端按回车继续导出状态。');
  process.stdin.resume();
  await new Promise(resolve => process.stdin.once('data', resolve));

  await context.storageState({ path: './data/wechat_storage_state.json' });
  console.log('已导出: ./data/wechat_storage_state.json');
  await browser.close();
})();
