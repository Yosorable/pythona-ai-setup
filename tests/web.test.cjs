const { test } = require('node:test');
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const render = language => execFileSync(process.env.TEST_PYTHON || 'python3', ['-c',
  'import sys; from ai_setup.page import render_page; print(render_page(sys.argv[1], preview=True))', language],
  { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } });
const waitPage = (page, name) => page.waitForFunction(name => document.documentElement.dataset.page === name
  && !document.getElementById('new').disabled, name);

test('home, add, and edit have one commit action; cancel discards drafts and edits', async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setContent(render('en'));
    await waitPage(page, 'home');
    assert.equal(await page.locator('.profile').count(), 0);
    assert.equal(await page.locator('#detail').isVisible(), false);
    await page.locator('#new').click();
    await waitPage(page, 'new');
    assert.equal(await page.locator('#home').isVisible(), false);
    assert.equal(await page.locator('#files').isChecked(), true);
    assert.equal(await page.locator('#browser').isChecked(), false);
    assert.equal(await page.locator('#availability').getAttribute('data-kind'), 'unavailable');
    await page.locator('#name').fill('Cancelled draft');
    await page.locator('#back').click();
    await waitPage(page, 'home');
    assert.equal(await page.locator('.profile').count(), 0);
    await page.locator('#new').click();
    await waitPage(page, 'new');
    assert.equal(await page.locator('#name').inputValue(), 'Apple On-Device Model');
    await page.locator('#name').fill('');
    await page.locator('#install').click();
    assert.match(await page.locator('#error').innerText(), /Enter a connection name/);
    assert.equal(await page.locator('html').getAttribute('data-page'), 'new');
    await page.locator('#name').fill('Installed once');
    await page.waitForTimeout(650);
    assert.equal(await page.locator('#name').inputValue(), 'Installed once');
    await page.locator('#install').click();
    await waitPage(page, 'home');
    assert.equal(await page.locator('.profile').count(), 1);
    const profileID = await page.locator('.profile').getAttribute('data-id');
    await page.locator('.profile').click();
    await waitPage(page, 'edit');
    assert.equal(await page.locator('#install > span').first().innerText(), 'Save Changes');
    assert.equal(await page.locator('#backend').isDisabled(), true);
    await page.locator('#name').fill('Discarded edit');
    await page.locator('#back').click();
    await waitPage(page, 'home');
    assert.equal(await page.locator('.profile-name').innerText(), 'Installed once');
    await page.locator('.profile').click();
    await waitPage(page, 'edit');
    await page.locator('#name').fill('Saved edit');
    await page.locator('#install').click();
    await waitPage(page, 'home');
    assert.equal(await page.locator('.profile').count(), 1);
    assert.equal(await page.locator('.profile').getAttribute('data-id'), profileID);
    assert.equal(await page.locator('.profile-name').innerText(), 'Saved edit');
    await page.screenshot({ path: '/tmp/pythona-ai-setup-pages-home.png', fullPage: true });
    await page.evaluate(() => window.setupBridge.close());
    assert.equal(await page.locator('#new').isDisabled(), true);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('MLX creation tests independently, then edits the installed entry without leaking fields', async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true });
    await page.setContent(render('en'));
    await waitPage(page, 'home');
    await page.locator('#new').click();
    await waitPage(page, 'new');
    await page.locator('#install').click();
    await waitPage(page, 'home');
    await page.locator('#new').click();
    await waitPage(page, 'new');
    await page.locator('#backend').selectOption('mlx_lm');
    await page.waitForFunction(() => !document.getElementById('mlx-settings').hidden && !document.getElementById('test').disabled);
    assert.equal(await page.locator('#model-id').inputValue(), 'mlx-community/Qwen3-1.7B-4bit');
    assert.equal(await page.locator('#name').inputValue(), 'Qwen3-1.7B (MLX)');
    assert.match(await page.locator('.model-notice').innerText(), /older devices.*error/);
    await page.locator('#browser').check();
    await page.locator('#prompt').fill('<script>window.injected = true</script>');
    await page.locator('#test').click();
    await page.waitForFunction(() => document.getElementById('reply').textContent.includes('<script>'));
    assert.equal(await page.evaluate(() => window.injected), undefined);
    assert.equal(await page.locator('.profile').count(), 1);
    await page.setViewportSize({ width: 390, height: 420 });
    await page.locator('#reply').scrollIntoViewIfNeeded();
    const reply = await page.locator('#reply').boundingBox();
    assert.ok(reply.y + reply.height <= 420);
    await page.locator('#test').click();
    await page.locator('#test').click();
    await page.waitForFunction(() => document.getElementById('test-status').textContent === 'Test cancelled');
    await page.locator('#name').fill('My MLX model');
    await page.locator('#model-id').fill('example/Another-Qwen');
    await page.locator('#install').click();
    await waitPage(page, 'home');
    assert.equal(await page.locator('.profile').count(), 2);
    await page.locator('.profile').first().click();
    await waitPage(page, 'edit');
    assert.equal(await page.locator('#browser').isChecked(), false);
    assert.equal(await page.locator('.model-notice').isVisible(), false);
    await page.locator('#back').click();
    await waitPage(page, 'home');
    await page.locator('.profile').nth(1).click();
    await waitPage(page, 'edit');
    assert.equal(await page.locator('#model-id').inputValue(), 'example/Another-Qwen');
    assert.equal(await page.locator('#browser').isChecked(), true);
    assert.equal(await page.locator('#reply').innerText(), '');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: '/tmp/pythona-ai-setup-pages-mlx.png', fullPage: true });
  } finally { await browser.close(); }
});

test('home and detail render all app languages and a desktop layout without overflow', async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    for (const language of ['en', 'zh-Hans', 'zh-Hant', 'de', 'es', 'fr', 'ja', 'ko', 'ru']) {
      await page.goto('about:blank');
      await page.setContent(render(language));
      await waitPage(page, 'home');
      assert.equal(await page.locator('html').getAttribute('lang'), language);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, language);
      await page.locator('#new').click();
      await waitPage(page, 'new');
      await page.locator('#backend').selectOption('mlx_lm');
      await page.waitForFunction(() => !document.getElementById('mlx-settings').hidden);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, language);
    }
    await page.setViewportSize({ width: 1100, height: 850 });
    await page.emulateMedia({ colorScheme: 'dark' });
    await page.goto('about:blank');
    await page.setContent(render('en'));
    await waitPage(page, 'home');
    await page.locator('#new').click();
    await waitPage(page, 'new');
    const settings = await page.locator('.settings-panel').boundingBox();
    const probe = await page.locator('.test-panel').boundingBox();
    assert.equal(settings.y, probe.y);
    assert.ok(probe.x > settings.x + settings.width);
    await page.screenshot({ path: '/tmp/pythona-ai-setup-pages-desktop.png', fullPage: true });
  } finally { await browser.close(); }
});
