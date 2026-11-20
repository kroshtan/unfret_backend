// /pass: where the "Use on your phone" QR code on /my-checks points (<site>/pass#pass=ufk_…). On Android with the
// Unfret app installed, the app opens that link itself (a verified App Link: /.well-known/assetlinks.json) and this
// page never loads. Anywhere else (no app, an iPhone, a computer) it forwards to the link checker with the fragment
// as it is, so /check#pass=… asks "Use this pass here?" as it always has. The fragment never reaches the server, so
// the forward happens here; replace() keeps /pass out of the history, so Back doesn't bounce through it again.
location.replace(`/check${location.hash}`);
