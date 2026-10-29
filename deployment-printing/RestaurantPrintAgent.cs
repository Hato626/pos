// Windows 10 / .NET Framework 4.8. Uses the installed Windows driver, including USB label printers.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Printing;
using System.Globalization;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

class TicketRenderer
{
    public class Row { public string Name, Quantity = "", Amount = ""; }
    public readonly List<Row> Rows = new List<Row>();
    public readonly List<List<Row>> Pages = new List<List<Row>>();
    readonly Dictionary<string, object> payload;
    readonly bool kitchen;
    public readonly float Width, Height;
    public readonly int RowsPerPage;
    float RightEdge { get { return Width - 2; } }
    internal float ContentWidth { get { return Width - 4; } }
    bool Narrow { get { return Width < 45; } }
    public static readonly Font Body = new Font("Microsoft YaHei", 7.2f);
    static readonly Font Small = new Font("Microsoft YaHei", 6.2f);
    static readonly Font Bold = new Font("Microsoft YaHei", 8, FontStyle.Bold);
    internal static readonly StringFormat Format = CreateFormat();
    static StringFormat CreateFormat()
    {
        StringFormat format = (StringFormat)StringFormat.GenericTypographic.Clone();
        format.FormatFlags = StringFormatFlags.NoWrap | StringFormatFlags.FitBlackBox;
        format.Trimming = StringTrimming.EllipsisCharacter;
        return format;
    }

    public TicketRenderer(Dictionary<string, object> data, bool isKitchen)
    {
        payload = data; kitchen = isKitchen;
        Dictionary<string,object> paper = data.ContainsKey("paper") ? data["paper"] as Dictionary<string,object> : null;
        Width = paper == null ? 60 : Number(paper,"width_mm");
        Height = paper == null ? 40 : Number(paper,"height_mm");
        if(!((Width==50 && Height==60) || (Width==30 && Height==60) || (Width==60 && Height==50) || (Width==60 && Height==30) || (Width==60 && Height==40)))
            throw new InvalidOperationException("不支持的后台纸张尺寸");
        RowsPerPage = Math.Max(1,(int)Math.Floor((Height-20.7f)/3));
        using (Bitmap bitmap = new Bitmap(600, 400))
        using (Graphics g = Graphics.FromImage(bitmap))
        {
            g.PageUnit = GraphicsUnit.Millimeter;
            AddWide(g, "桌台：" + Text(data, "table_name"));
            if (Text(data, "customer_name") != "") AddWide(g, "顾客：" + Text(data, "customer_name"));
            foreach (object raw in Array(data, "lines"))
            {
                Dictionary<string, object> item = (Dictionary<string, object>)raw;
                string action = Text(item, "action");
                string name = (kitchen && action != "" ? "[" + action + "] " : "") + Text(item, "name");
                List<string> parts = Wrap(g, name, Narrow ? ContentWidth : kitchen ? Width-13 : Width-28, Body);
                for (int i = 0; i < parts.Count; i++) Rows.Add(new Row { Name = parts[i], Quantity = !Narrow && i == 0 ? Text(item, "quantity") : "", Amount = !Narrow && !kitchen && i == 0 ? Money(Number(item, "price_cents") * Number(item, "quantity")) : "" });
                if(Narrow) AddWide(g,"数量："+Text(item,"quantity")+(!kitchen ? " 金额："+Money(Number(item,"price_cents")*Number(item,"quantity")) : ""));
                if (Text(item, "notes") != "") AddWide(g, "备注：" + Text(item, "notes"));
            }
            if (Text(data, "order_notes") != "") AddWide(g, "订单备注：" + Text(data, "order_notes"));
            if (!kitchen)
            {
                if (Number(data, "delivery_fee_cents") != 0) AddWide(g, "配送费：" + Money(Number(data, "delivery_fee_cents")));
                if (Number(data, "loyalty_discount_cents") != 0) AddWide(g, "优惠：-" + Money(Number(data, "loyalty_discount_cents")));
                if (Number(data, "tip_amount_cents") != 0) AddWide(g, "小费：" + Money(Number(data, "tip_amount_cents")));
                Dictionary<string, int> methods = new Dictionary<string, int>();
                foreach (object raw in Array(data, "payments"))
                {
                    Dictionary<string, object> payment = (Dictionary<string, object>)raw;
                    string method = Text(payment, "payment_method");
                    if (!methods.ContainsKey(method)) methods[method] = 0;
                    methods[method] += Number(payment, "amount_cents");
                }
                foreach (KeyValuePair<string,int> method in methods) AddWide(g, "支付：" + MethodName(method.Key) + " " + Money(method.Value));
                if (methods.Count == 0 && Text(data, "payment_method") != "") AddWide(g, "支付：" + MethodName(Text(data, "payment_method")));
            }
        }
        for (int i = 0; i < Rows.Count; i += RowsPerPage) Pages.Add(Rows.GetRange(i, Math.Min(RowsPerPage, Rows.Count - i)));
        if (Pages.Count == 0) Pages.Add(new List<Row>());
    }
    static string MethodName(string value)
    {
        switch (value) { case "cash": return "现金"; case "terminal": return "刷卡"; case "stripe": return "线上支付"; case "mixed": return "混合支付"; default: return value; }
    }
    string Money(int cents)
    {
        string currency = Text(payload, "currency_code");
        string symbol = currency == "CNY" ? "¥" : currency == "EUR" ? "€" : currency == "USD" ? "$" : currency + " ";
        return symbol + (cents / 100m).ToString("0.00", CultureInfo.InvariantCulture);
    }
    void AddWide(Graphics g, string text) { foreach (string line in Wrap(g, text, ContentWidth, Body)) Rows.Add(new Row { Name = line }); }
    public static List<string> Wrap(Graphics g, string text, float width, Font font)
    {
        List<string> result = new List<string>();
        foreach (string paragraph in text.Replace("\r", "").Split('\n'))
        {
            string line = "";
            TextElementEnumerator enumerator = StringInfo.GetTextElementEnumerator(paragraph);
            while (enumerator.MoveNext())
            {
                string ch = enumerator.GetTextElement();
                if (line.Length > 0 && g.MeasureString(line + ch, font, 1000, Format).Width > width) { result.Add(line); line = ""; }
                line += ch;
            }
            result.Add(line);
        }
        return result;
    }
    static void Draw(Graphics g, string value, Font font, float x, float y, float width)
    { g.DrawString(value, font, Brushes.Black, new RectangleF(x, y, width, 3.5f), Format); }
    static void Right(Graphics g, string value, Font font, float right, float y, float width)
    {
        using (StringFormat format = (StringFormat)Format.Clone())
        { format.Alignment = StringAlignment.Far; g.DrawString(value, font, Brushes.Black, new RectangleF(right - width, y, width, 3.5f), format); }
    }
    string TimeLabel()
    {
        DateTimeOffset date;
        string value = Text(payload, kitchen ? "created_at" : "paid_at");
        if (!DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal, out date)) return "";
        // Windows uses Windows timezone identifiers. China/Japan are the deployed venue defaults.
        string zone = Text(payload, "timezone");
        string winZone = zone == "Asia/Tokyo" ? "Tokyo Standard Time" : zone == "Asia/Shanghai" ? "China Standard Time" : "";
        try { if (winZone != "") date = TimeZoneInfo.ConvertTime(date, TimeZoneInfo.FindSystemTimeZoneById(winZone)); else return date.UtcDateTime.ToString("MM-dd HH:mm") + " UTC"; } catch { }
        return date.ToString(Narrow ? "HH:mm" : "MM-dd HH:mm");
    }
    public void DrawPage(Graphics g, int page)
    {
        g.PageUnit = GraphicsUnit.Millimeter;
        g.SmoothingMode = SmoothingMode.HighQuality;
        Draw(g, Text(payload, "title"), Narrow ? Body : Bold, 2, 1.6f, ContentWidth);
        Draw(g, Text(payload, "restaurant_name"), Small, 2, 5.1f, ContentWidth);
        Draw(g, "#" + Text(payload, "order_id"), Small, 2, 8.2f, Narrow ? 10 : Width-35);
        Right(g, TimeLabel(), Small, RightEdge, 8.2f, Narrow ? 15 : 31);
        using (Pen pen = new Pen(Color.Black, .15f)) g.DrawLine(pen, 2, 11.5f, RightEdge, 11.5f);
        int row = 0;
        foreach (Row item in Pages[page])
        {
            float y = 12.2f + row * 3;
            float width = item.Quantity == "" && item.Amount == "" ? ContentWidth : kitchen ? Width-13 : Width-28;
            Draw(g, item.Name, Body, 2, y, width);
            Right(g, item.Quantity, Body, kitchen ? RightEdge : Width-18, y, 7);
            if (!kitchen) Right(g, item.Amount, Small, RightEdge, y, 15);
            row++;
        }
        float footer=Height-8;
        using (Pen pen = new Pen(Color.Black, .15f)) g.DrawLine(pen, 2, footer, RightEdge, footer);
        if (kitchen) Draw(g, Narrow ? "制作联 · 非结算" : "制作联 · 非结算凭证", Small, 2, footer+.5f, ContentWidth);
        else {
            if(Narrow) Draw(g,page==Pages.Count-1 ? "合计 "+Money(Number(payload,"total_cents")) : "续下张",Body,2,footer+.2f,ContentWidth);
            else { Draw(g, page == Pages.Count - 1 ? "实收合计" : "续下张", Bold, 2, footer+.2f, 20); if (page == Pages.Count - 1) Right(g, Money(Number(payload, "total_cents")), Bold, RightEdge, footer+.2f, Width-25); }
        }
        Draw(g, Narrow ? (kitchen ? "核对菜品" : "非税务发票") : kitchen ? "请核对菜品与备注" : "消费凭证 · 非税务发票", Small, 2, Height-4, Width-13);
        Right(g, (page + 1) + "/" + Pages.Count, Small, RightEdge, Height-4, 9);
    }
    public void Print(string printer, string jobId)
    {
        using (PrintDocument document = new PrintDocument())
        {
            document.PrinterSettings.PrinterName = printer;
            if (!document.PrinterSettings.IsValid) throw new InvalidOperationException("Windows 中找不到绑定的打印队列：" + printer);
            PaperSize size = null;
            int paperWidth=(int)Math.Round(Width/25.4f*100),paperHeight=(int)Math.Round(Height/25.4f*100);
            foreach (PaperSize candidate in document.PrinterSettings.PaperSizes)
                if (Math.Abs(candidate.Width - paperWidth) < 3 && Math.Abs(candidate.Height - paperHeight) < 3) { size = candidate; break; }
            document.DefaultPageSettings.PaperSize = size ?? new PaperSize(Width+"x"+Height+" mm",paperWidth,paperHeight);
            document.DefaultPageSettings.Landscape = false;
            document.DefaultPageSettings.Margins = new Margins(0, 0, 0, 0);
            document.DocumentName = "Satisfecho job " + jobId;
            document.PrintController = new StandardPrintController();
            int page = 0;
            document.PrintPage += delegate(object sender, PrintPageEventArgs e) {
                e.Graphics.PageUnit = GraphicsUnit.Millimeter;
                e.Graphics.TranslateTransform(-e.PageSettings.HardMarginX * .254f, -e.PageSettings.HardMarginY * .254f);
                DrawPage(e.Graphics, page++);
                e.HasMorePages = page < Pages.Count;
            };
            document.Print(); // Success means accepted by Windows, never proof of physical output.
        }
    }
    public static string Text(Dictionary<string,object> d, string key) { return d.ContainsKey(key) && d[key] != null ? Convert.ToString(d[key], CultureInfo.InvariantCulture) : ""; }
    public static int Number(Dictionary<string,object> d, string key) { int value; return int.TryParse(Text(d,key), out value) ? value : 0; }
    public static IEnumerable Array(Dictionary<string,object> d, string key) { return d.ContainsKey(key) && d[key] is IEnumerable && !(d[key] is string) ? (IEnumerable)d[key] : new object[0]; }
}

class AgentWindow : Form
{
    const string BaseUrl = "https://drink-admin.354612.xyz";
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 4 * 1024 * 1024 };
    static readonly string DataDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SatisfechoPrinting");
    TextBox token = new TextBox { UseSystemPasswordChar=true, Width=500 };
    TextBox status = new TextBox { Multiline=true, ReadOnly=true, ScrollBars=ScrollBars.Vertical, Width=500, Height=180 };
    Button start = new Button { Text="连接并启动", Width=120 };
    Button stop = new Button { Text="停止", Enabled=false, Width=100 };
    CancellationTokenSource cancellation;
    public AgentWindow()
    {
        Text="餐厅打印助手 - 后厨单 / 结算单"; ClientSize=new Size(550,400); FormBorderStyle=FormBorderStyle.FixedDialog; MaximizeBox=false;
        FlowLayoutPanel flow = new FlowLayoutPanel { Dock=DockStyle.Fill, Padding=new Padding(20), FlowDirection=FlowDirection.TopDown, WrapContents=false };
        flow.Controls.Add(new Label { Text="保持本程序运行，自动打印下单后的后厨单、结账后的结算单。", AutoSize=true });
        flow.Controls.Add(new Label { Text="餐厅绑定页生成的助手令牌（仅保存在当前 Windows 用户下）：", AutoSize=true, Margin=new Padding(0,15,0,5) });
        flow.Controls.Add(token);
        FlowLayoutPanel buttons = new FlowLayoutPanel { Width=500, Height=40, Margin=new Padding(0,10,0,0) };
        buttons.Controls.Add(start); buttons.Controls.Add(stop);
        Button binding = new Button { Text="打开打印机绑定页", Width=160 };
        binding.Click += delegate { System.Diagnostics.Process.Start(BaseUrl + "/settings?section=printing"); };
        buttons.Controls.Add(binding); flow.Controls.Add(buttons); flow.Controls.Add(status);
        flow.Controls.Add(new Label { Text="纸张按后台每台打印机设置。助手 v2，成功提交不代表实际出纸。", AutoSize=true });
        Controls.Add(flow);
        Directory.CreateDirectory(DataDir);
        try { token.Text=Encoding.UTF8.GetString(ProtectedData.Unprotect(File.ReadAllBytes(Path.Combine(DataDir,"agent.dat")),null,DataProtectionScope.CurrentUser)); } catch { }
        start.Click += delegate { StartAgent(); };
        stop.Click += delegate { if(cancellation!=null)cancellation.Cancel(); };
        FormClosing += delegate(object sender, FormClosingEventArgs e) {
            if(cancellation!=null && !cancellation.IsCancellationRequested && MessageBox.Show("关闭后将停止自动打印，是否关闭？","打印助手",MessageBoxButtons.YesNo)!=DialogResult.Yes) { e.Cancel=true; return; }
            if(cancellation!=null)cancellation.Cancel();
        };
    }
    void Log(string message)
    {
        if (IsDisposed || !IsHandleCreated) return;
        BeginInvoke((Action)delegate { if (status.TextLength > 12000) status.Clear(); status.AppendText(DateTime.Now.ToString("HH:mm:ss") + " " + message + Environment.NewLine); });
    }
    void StartAgent()
    {
        string raw = token.Text.Trim();
        if(raw.Length<20) { MessageBox.Show("请先从本餐厅的绑定页生成并复制助手令牌。"); return; }
        File.WriteAllBytes(Path.Combine(DataDir,"agent.dat"),ProtectedData.Protect(Encoding.UTF8.GetBytes(raw),null,DataProtectionScope.CurrentUser));
        cancellation=new CancellationTokenSource(); start.Enabled=false; token.Enabled=false; stop.Enabled=true;
        Task.Run(delegate {
            try { Run(raw,cancellation.Token); }
            catch(Exception) { Log("助手已停止，请检查网络或重新连接。"); }
            finally { if(!IsDisposed && IsHandleCreated) BeginInvoke((Action)delegate { start.Enabled=true; token.Enabled=true; stop.Enabled=false; }); }
        });
    }
    static object Request(HttpClient client, string path, object body)
    {
        using (HttpRequestMessage request = new HttpRequestMessage(body==null ? HttpMethod.Get : HttpMethod.Post, BaseUrl + "/api/print-workflow" + path))
        {
            if(body!=null) request.Content=new StringContent(Json.Serialize(body),Encoding.UTF8,"application/json");
            using(HttpResponseMessage response=client.SendAsync(request).GetAwaiter().GetResult())
            {
                if(response.StatusCode==HttpStatusCode.Unauthorized) throw new UnauthorizedAccessException();
                response.EnsureSuccessStatusCode();
                return Json.DeserializeObject(response.Content.ReadAsStringAsync().GetAwaiter().GetResult());
            }
        }
    }
    string LedgerPath(string raw)
    {
        using(SHA256 sha=SHA256.Create()) return Path.Combine(DataDir,BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(raw))).Replace("-","").Substring(0,16)+"-ack.json");
    }
    static void SaveLedger(string path, Dictionary<string,object> data)
    {
        string temporary=path+".new";
        using(FileStream stream=new FileStream(temporary,FileMode.Create,FileAccess.Write,FileShare.None))
        using(StreamWriter writer=new StreamWriter(stream,new UTF8Encoding(false)))
        { writer.Write(Json.Serialize(data)); writer.Flush(); stream.Flush(true); }
        if(File.Exists(path)) File.Replace(temporary,path,null); else File.Move(temporary,path);
    }
    void Run(string raw, CancellationToken cancel)
    {
        ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
        string ledger=LedgerPath(raw);
        using(HttpClient client=new HttpClient())
        {
            client.Timeout=TimeSpan.FromSeconds(15); client.DefaultRequestHeaders.Authorization=new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer",raw);
            Log("连接中；请在绑定页把两条流程绑定到本机的实际打印队列。");
            int turn=0;
            while(!cancel.IsCancellationRequested)
            {
                try
                {
                    if(File.Exists(ledger))
                    {
                        Dictionary<string,object> ack=(Dictionary<string,object>)Json.DeserializeObject(File.ReadAllText(ledger));
                        if(TicketRenderer.Text(ack,"status")=="printing")
                        {
                            Log("上次任务 #"+TicketRenderer.Text(ack,"id")+" 结果不确定，请在绑定页核对并补打。当前任务不会自动重打。");
                            ack["status"]="failed"; ack["error_message"]="助手中断，出纸结果不确定，请现场核对后手动补打";
                            SaveLedger(ledger,ack);
                        }
                        Request(client,"/agent/jobs/"+TicketRenderer.Text(ack,"id")+"/complete",new {status=TicketRenderer.Text(ack,"status"),error_message=TicketRenderer.Text(ack,"error_message")});
                        File.Delete(ledger); Log("任务状态已同步。");
                    }
                    if(turn++ % 5==0)
                    {
                        List<string> printers=new List<string>(); foreach(string printer in PrinterSettings.InstalledPrinters) printers.Add(printer);
                        Dictionary<string,object> heartbeat=(Dictionary<string,object>)Request(client,"/agent/heartbeat",new {printers=printers,protocol_version=2});
                        if(!(bool)heartbeat["bound"]) { Log("助手已连接，尚未被本餐厅绑定；请打开绑定页选择本助手并保存。"); cancel.WaitHandle.WaitOne(10000); continue; }
                    }
                    object[] jobs=(object[])Request(client,"/agent/jobs?protocol_version=2",null);
                    foreach(object rawJob in jobs)
                    {
                        Dictionary<string,object> job=(Dictionary<string,object>)rawJob;
                        string id=TicketRenderer.Text(job,"id");
                        // Persist BEFORE touching the spooler. A crash is treated as uncertain, never an automatic reprint.
                        Dictionary<string,object> ack=new Dictionary<string,object> { {"id",id},{"status","printing"},{"error_message",""} };
                        SaveLedger(ledger,ack);
                        try
                        {
                            TicketRenderer renderer=new TicketRenderer((Dictionary<string,object>)job["payload"],TicketRenderer.Text(job,"job_type")=="kitchen");
                            renderer.Print(TicketRenderer.Text(job,"windows_printer"),id);
                            ack["status"]="done"; Log("任务 #"+id+" 已提交 Windows 打印队列（"+renderer.Width+"×"+renderer.Height+" mm，"+renderer.Pages.Count+" 张）。");
                        }
                        catch(Exception)
                        {
                            ack["status"]="failed"; ack["error_message"]="Windows 打印失败，请核对驱动、绑定的队列、纸张和打印机状态；确认出纸情况后补打";
                            Log("任务 #"+id+" 打印失败，请到绑定页核对后补打。");
                        }
                        SaveLedger(ledger,ack);
                        Request(client,"/agent/jobs/"+id+"/complete",new {status=ack["status"],error_message=ack["error_message"]});
                        File.Delete(ledger);
                    }
                }
                catch(UnauthorizedAccessException) { Log("令牌无效或已撤销，请重新生成令牌。"); return; }
                catch(Exception) { Log("网络或服务暂不可用，稍后重试；不会自动重打已提交任务。"); }
                cancel.WaitHandle.WaitOne(2000);
            }
            Log("已停止自动打印。");
        }
    }
}

static class Program
{
    [STAThread]
    static void Main(string[] args)
    {
        if(args.Length==2 && args[0]=="--self-test") { SelfTest(args[1]); return; }
        bool created;
        using(Mutex mutex=new Mutex(true,"Local\\SatisfechoUsbPrinterAgent",out created))
        {
            if(!created) { MessageBox.Show("打印助手已经运行，请查看已打开的窗口。"); return; }
            Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false); Application.Run(new AgentWindow());
        }
    }
    static void SelfTest(string output)
    {
        Directory.CreateDirectory(output);
        List<object> lines=new List<object>();
        for(int i=0;i<8;i++) lines.Add(new Dictionary<string,object>{{"name","菜品"+i+"招牌牛肉饭配新鲜时蔬及特别制作番茄酱"},{"quantity",i+1},{"price_cents",2800},{"notes","少油少盐，不放辣椒。酱汁请单独装。"},{"action","加菜"}});
        Dictionary<string,object> payload=new Dictionary<string,object>{{"title","后厨单"},{"restaurant_name","中文示范餐厅"},{"table_name","大厅08号桌"},{"order_id",321},{"currency_code","CNY"},{"timezone","Asia/Shanghai"},{"created_at","2026-10-28T14:30:00Z"},{"paid_at","2026-10-28T15:00:00Z"},{"lines",lines.ToArray()},{"total_cents",100800},{"payment_method","cash"}};
        StringBuilder report=new StringBuilder();
        foreach(int[] paper in new int[][] { new int[]{50,60},new int[]{30,60},new int[]{60,50},new int[]{60,30},new int[]{60,40} })
        foreach(bool kitchen in new bool[]{true,false})
        {
            payload["paper"]=new Dictionary<string,object>{{"width_mm",paper[0]},{"height_mm",paper[1]}};
            payload["title"]=kitchen?"后厨单":"结算单 · 已付款";
            TicketRenderer renderer=new TicketRenderer(payload,kitchen);
            if(renderer.Pages.Count<2) throw new Exception("Pagination failed");
            if(kitchen && renderer.Rows.Exists(r=>r.Amount!="")) throw new Exception("Kitchen price leak");
            int count=0;
            for(int i=0;i<renderer.Pages.Count;i++)
            {
                if(renderer.Pages[i].Count>renderer.RowsPerPage) throw new Exception("Vertical overflow");
                count+=renderer.Pages[i].Count;
                using(Bitmap bitmap=new Bitmap(paper[0]*8,paper[1]*8))
                {
                    bitmap.SetResolution(203.2f,203.2f);
                    using(Graphics graphics=Graphics.FromImage(bitmap)) {
                        graphics.Clear(Color.White); renderer.DrawPage(graphics,i);
                        graphics.PageUnit=GraphicsUnit.Millimeter;
                        foreach(TicketRenderer.Row row in renderer.Pages[i]) {
                            float width=row.Quantity=="" && row.Amount=="" ? renderer.ContentWidth : kitchen ? renderer.Width-13 : renderer.Width-28;
                            if(graphics.MeasureString(row.Name,TicketRenderer.Body,1000,TicketRenderer.Format).Width>width+.1f) throw new Exception("Horizontal overflow");
                        }
                    }
                    bitmap.Save(Path.Combine(output,paper[0]+"x"+paper[1]+"-"+(kitchen?"kitchen":"receipt")+"-"+(i+1)+".png"));
                }
            }
            if(count!=renderer.Rows.Count) throw new Exception("Rows lost during pagination");
            report.AppendLine(paper[0]+"x"+paper[1]+" "+(kitchen?"kitchen":"receipt")+" pages="+renderer.Pages.Count+" rows="+renderer.Rows.Count+" capacity="+renderer.RowsPerPage);
        }
        File.WriteAllText(Path.Combine(output,"self-test.txt"),report.ToString(),Encoding.UTF8);
    }
}
