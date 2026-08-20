using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace FunPayOperations.AuthHelper;

internal static class Program
{
    private const string FunPayUri = "https://funpay.com/";
    private const string ResultName = "session-result.dpapi";
    private static readonly HashSet<string> AdditionalAuthHosts = new(StringComparer.OrdinalIgnoreCase)
    {
        "id.vk.com",
        "oauth.vk.com",
    };

    [STAThread]
    private static int Main(string[] args)
    {
        if (args.SequenceEqual(new[] { "--runtime-status" }))
        {
            return RuntimeAvailable() ? 0 : 2;
        }
        if (!TryParseProfile(args, out var profile, out var smoke))
        {
            return 64;
        }
        if (!RuntimeAvailable())
        {
            return 2;
        }
        ApplicationConfiguration.Initialize();
        using var form = new AuthWindow(profile, smoke);
        Application.Run(form);
        return form.ExitCode;
    }

    private static bool RuntimeAvailable()
    {
        try
        {
            return !string.IsNullOrWhiteSpace(CoreWebView2Environment.GetAvailableBrowserVersionString());
        }
        catch (WebView2RuntimeNotFoundException)
        {
            return false;
        }
    }

    private static bool TryParseProfile(string[] args, out string profile, out bool smoke)
    {
        profile = string.Empty;
        smoke = false;
        if (args.Length is < 2 or > 3 || args[0] != "--profile-dir") return false;
        if (args.Length == 3 && args[2] != "--smoke") return false;
        var candidate = Path.GetFullPath(args[1]);
        var localData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var expectedParent = Path.Combine(localData, "FunPay Operations", "data");
        if (!Path.GetDirectoryName(candidate)!.Equals(expectedParent, StringComparison.OrdinalIgnoreCase)
            || !Path.GetFileName(candidate).StartsWith("auth-temp-", StringComparison.Ordinal)
            || !Directory.Exists(candidate)) return false;
        profile = candidate;
        smoke = args.Length == 3;
        return true;
    }

    private sealed class AuthWindow : Form
    {
        private readonly string _profile;
        private readonly bool _smoke;
        private readonly Label _status = new() { Dock = DockStyle.Bottom, Height = 32, Text = "Ожидаем вход в FunPay…", TextAlign = ContentAlignment.MiddleLeft, Padding = new Padding(12, 0, 12, 0) };
        private readonly WebView2 _webView = new() { Dock = DockStyle.Fill };
        private readonly System.Windows.Forms.Timer _smokeTimeout = new() { Interval = 30_000 };
        private bool _saved;
        private bool _finished;

        public int ExitCode { get; private set; } = 1;

        public AuthWindow(string profile, bool smoke)
        {
            _profile = profile;
            _smoke = smoke;
            Text = "FunPay Operations for World of Warcraft Mythic+ — Авторизация FunPay";
            Width = 980;
            Height = 720;
            MinimizeBox = true;
            Controls.Add(_webView);
            Controls.Add(_status);
            Shown += async (_, _) => await InitializeAsync();
            FormClosing += (_, _) => ClearCookies();
            if (_smoke)
            {
                _smokeTimeout.Tick += (_, _) => Finish(3);
                Shown += (_, _) => _smokeTimeout.Start();
            }
        }

        private async Task InitializeAsync()
        {
            try
            {
                var environment = await CoreWebView2Environment.CreateAsync(null, _profile);
                await _webView.EnsureCoreWebView2Async(environment);
                var core = _webView.CoreWebView2;
                core.Settings.AreDevToolsEnabled = false;
                core.Settings.AreDefaultContextMenusEnabled = false;
                core.NavigationStarting += (_, args) =>
                {
                    if (!AllowedFunPayUrl(args.Uri)) args.Cancel = true;
                };
                core.NewWindowRequested += (_, args) =>
                {
                    args.Handled = true;
                    if (AllowedFunPayUrl(args.Uri)) core.Navigate(args.Uri);
                };
                core.DownloadStarting += (_, args) => args.Cancel = true;
                core.PermissionRequested += (_, args) => args.State = CoreWebView2PermissionState.Deny;
                core.LaunchingExternalUriScheme += (_, args) => args.Cancel = true;
                core.NavigationCompleted += async (_, args) => await NavigationCompletedAsync(args.IsSuccess);
                core.Navigate(FunPayUri);
            }
            catch (Exception)
            {
                _status.Text = "Не удалось открыть окно авторизации FunPay.";
                Finish(3);
            }
        }

        private async Task NavigationCompletedAsync(bool success)
        {
            if (!success || _webView.CoreWebView2 is null) return;
            if (_smoke)
            {
                Finish(0);
                return;
            }
            var cookies = await _webView.CoreWebView2.CookieManager.GetCookiesAsync(FunPayUri);
            var selected = cookies
                .Where(cookie => cookie.Name is "golden_key" or "golden_seal")
                .GroupBy(cookie => cookie.Name, StringComparer.Ordinal)
                .ToDictionary(group => group.Key, group => group.Last().Value, StringComparer.Ordinal);
            if (!selected.TryGetValue("golden_key", out var key) || string.IsNullOrWhiteSpace(key)
                || !selected.TryGetValue("golden_seal", out var seal) || string.IsNullOrWhiteSpace(seal)) return;
            try
            {
                var payload = JsonSerializer.Serialize(new Dictionary<string, string>
                {
                    ["golden_key"] = key,
                    ["golden_seal"] = seal,
                });
                var protectedBytes = ProtectedData.Protect(Encoding.UTF8.GetBytes(payload), null, DataProtectionScope.CurrentUser);
                File.WriteAllText(Path.Combine(_profile, ResultName), Convert.ToBase64String(protectedBytes), Encoding.ASCII);
                _saved = true;
                _status.Text = "Проверяем авторизацию…";
                Finish(0);
            }
            catch (Exception)
            {
                _status.Text = "Не удалось подготовить локальную проверку авторизации.";
                Finish(3);
            }
        }

        private void Finish(int exitCode)
        {
            if (_finished || IsDisposed) return;
            _finished = true;
            _smokeTimeout.Stop();
            ExitCode = exitCode;
            BeginInvoke((MethodInvoker)Close);
        }

        private void ClearCookies()
        {
            _smokeTimeout.Stop();
            try { _webView.CoreWebView2?.CookieManager.DeleteAllCookies(); }
            catch (Exception) { }
            _webView.Dispose();
            if (!_saved && ExitCode == 0 && !_smoke) ExitCode = 1;
        }

        private static bool AllowedFunPayUrl(string value)
        {
            return Uri.TryCreate(value, UriKind.Absolute, out var uri)
                && uri.Scheme == Uri.UriSchemeHttps
                && (uri.Host.Equals("funpay.com", StringComparison.OrdinalIgnoreCase)
                    || uri.Host.EndsWith(".funpay.com", StringComparison.OrdinalIgnoreCase)
                    || AdditionalAuthHosts.Contains(uri.Host));
        }
    }
}
