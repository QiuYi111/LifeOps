import cloud from '@lafjs/cloud'
import axios from 'axios'
import crypto from 'crypto'

export default async function (ctx: FunctionContext) {
  const { body } = ctx;
  const ENCRYPT_KEY = process.env.FEISHU_ENCRYPT_KEY;

  // 1. 解密 & 握手 (保持不变)
  let eventData = body;
  if (body.encrypt) {
    if (!ENCRYPT_KEY) return { msg: "Configuration Error" };
    try {
      eventData = decryptFeishuData(body.encrypt, ENCRYPT_KEY);
    } catch (err) { return { msg: "Decryption Failed" }; }
  }
  if (eventData.challenge) return { challenge: eventData.challenge };

  // 2. 只有这一条路径：无脑转发给 GitHub
  if (eventData.header && eventData.header.event_type === 'im.message.receive_v1') {
    try {
      const content = JSON.parse(eventData.event.message.content);
      const text = content.text;
      const open_id = eventData.event.sender.sender_id.open_id;

      console.log('🚀 Forwarding to GitHub:', text);

      const repo = process.env.GITHUB_REPO;
      const token = process.env.GITHUB_TOKEN;

      // 所有的判断逻辑都交给 Claude 去做
      await axios.post(
        `https://api.github.com/repos/${repo}/dispatches`,
        {
          event_type: 'feishu_trigger',
          client_payload: { user_command: text, user_id: open_id }
        },
        { headers: { 'Authorization': `token ${token}`, 'Accept': 'application/vnd.github.v3+json' } }
      );

    } catch (err) { console.error('Error:', err); }
  }
  return { msg: 'ok' }
}

// (decryptFeishuData 函数保持不变，这里省略)
function decryptFeishuData(encryptStr, encryptKey) {
  const key = crypto.createHash('sha256').update(encryptKey).digest();
  const buff = Buffer.from(encryptStr, 'base64');
  const iv = buff.subarray(0, 16);
  const encryptedData = buff.subarray(16);
  const decipher = crypto.createDecipheriv('aes-256-cbc', key, iv);
  let decrypted = decipher.update(encryptedData);
  decrypted = Buffer.concat([decrypted, decipher.final()]);
  return JSON.parse(decrypted.toString());
}
