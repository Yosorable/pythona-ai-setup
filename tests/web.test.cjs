const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const render = language => execFileSync(process.env.TEST_PYTHON || 'python3', ['-c',
  'import sys; from ai_setup.page import render_page; print(render_page(sys.argv[1], preview=True))', language],
  { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });

test('browser page installs and updates with an unavailable model, preserves edits, and displays replies safely', async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setContent(render('en'));
    await page.waitForFunction(() => document.documentElement.dataset.ready === 'true');
    assert.equal(await page.locator('#files').isChecked(), true);
    assert.equal(await page.locator('#browser').isChecked(), false);
    assert.equal(await page.locator('#availability').getAttribute('data-kind'), 'unavailable');
    await page.locator('#install').click();
    await page.waitForFunction(() => document.getElementById('install').textContent.includes('Update'));
    await page.locator('#name').fill('Edited while polling');
    await page.waitForTimeout(650);
    assert.equal(await page.locator('#name').inputValue(), 'Edited while polling');
    await page.locator('#install').click();
    assert.match(await page.locator('#installation').innerText(), /preview-provider/);
    await page.locator('#prompt').fill('<script>window.injected = true</script>');
    await page.locator('#test').click();
    await page.waitForFunction(() => document.getElementById('reply').textContent.includes('<script>'));
    assert.equal(await page.evaluate(() => window.injected), undefined);
    await page.setViewportSize({ width: 390, height: 420 });
    await page.locator('#reply').scrollIntoViewIfNeeded();
    const reply = await page.locator('#reply').boundingBox();
    assert.ok(reply.y + reply.height <= 420);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: '/tmp/pythona-ai-setup-web-mobile.png', fullPage: true });
    await page.locator('#test').click();
    await page.locator('#test').click();
    await page.waitForFunction(() => document.getElementById('test-status').textContent === 'Test cancelled');
    await page.locator('#close').click();
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('all app languages and the desktop dark layout render without horizontal overflow', async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    for (const language of ['en', 'zh-Hans', 'zh-Hant', 'de', 'es', 'fr', 'ja', 'ko', 'ru']) {
      await page.setContent(render(language));
      await page.waitForFunction(() => document.documentElement.dataset.ready === 'true');
      assert.equal(await page.locator('html').getAttribute('lang'), language);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, language);
    }
    await page.setViewportSize({ width: 1100, height: 850 });
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.setContent(render('en'));
    await page.waitForFunction(() => document.documentElement.dataset.ready === 'true');
    const settings = await page.locator('.settings-panel').boundingBox();
    const probe = await page.locator('.test-panel').boundingBox();
    assert.equal(settings.y, probe.y);
    assert.ok(probe.x > settings.x + settings.width);
    await page.screenshot({ path: '/tmp/pythona-ai-setup-web-desktop.png', fullPage: true });
  } finally { await browser.close(); }
});
