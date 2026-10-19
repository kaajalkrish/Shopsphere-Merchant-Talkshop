import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Check, RotateCcw, ShieldCheck, Sparkles, Truck } from "lucide-react";
import { money, niceDate, shop, ShopError, type Availability, type Product, type ProductDetail } from "../../api/shop";
import ProductTile from "../../components/shopsphere/ProductTile";
import { Badge, Button, Chip, Empty, Notice, Rating, Skeleton, Swatch } from "../../components/ui";
import { useCart } from "../../store/cart";
import { TALKSHOP_ENABLED, askTalkshop } from "../../talkshop/bridge";

function deliveryDate(days: number) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return niceDate(d.toISOString());
}

export default function ProductPage() {
  const { productId = "" } = useParams();
  const { setCart } = useCart();
  const [product, setProduct] = useState<ProductDetail | null | undefined>(undefined);
  const [avail, setAvail] = useState<Availability | null>(null);
  const [color, setColor] = useState<string | null>(null);
  const [size, setSize] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [added, setAdded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [related, setRelated] = useState<Product[]>([]);

  useEffect(() => {
    setProduct(undefined); setColor(null); setSize(null); setAdded(false); setError(null);
    shop.product(productId).then((p) => {
      setProduct(p);
      setColor(p.colors.find((c) => c.in_stock)?.name ?? p.colors[0]?.name ?? null);
      shop.products({ category: p.category, limit: 5 }).then((r) => setRelated(r.filter((x) => x.product_id !== p.product_id).slice(0, 4)));
    }).catch(() => setProduct(null));
  }, [productId]);

  useEffect(() => {
    if (!product) return;
    shop.availability(product.product_id, size, color).then(setAvail).catch(() => {});
  }, [product, size, color]);

  const hasSizes = (product?.sizes.length ?? 0) > 0;
  const image = useMemo(() => product?.colors.find((c) => c.name === color)?.image_url ?? product?.image_url, [product, color]);
  const selected = avail?.selected;
  const ready = !!color && (!hasSizes || !!size) && !!selected?.available;

  if (product === null) return <Empty title="We couldn't find that product"><Link to="/" className="underline">Back to ShopSphere</Link></Empty>;
  if (!product) return (
    <div className="mx-auto max-w-[1400px] px-4 sm:px-6 pt-10 grid lg:grid-cols-2 gap-12">
      <Skeleton className="aspect-square" /><div className="flex flex-col gap-4"><Skeleton className="h-8 w-2/3" /><Skeleton className="h-6 w-1/3" /><Skeleton className="h-40" /></div>
    </div>
  );

  const add = async () => {
    if (!selected?.sku) return;
    setAdding(true); setError(null);
    try {
      const cart = await shop.addToCart(selected.sku, 1);
      setCart(cart, { bump: true });
      setAdded(true);
    } catch (e) {
      setError(e instanceof ShopError ? e.message : "Couldn't add to cart. Please try again.");
    } finally {
      setAdding(false);
    }
  };

  const sizeChips = avail?.sizes ?? product.sizes.map((s) => ({ size: s, available: true }));
  const label = product.option_label;

  return (
    <div className="mx-auto max-w-[1400px] px-4 sm:px-6 pt-8">
      <nav className="text-xs text-muted mb-6">
        <Link to="/" className="hover:underline">Home</Link> / <Link to={`/c/${product.department}`} className="hover:underline capitalize">{product.department}</Link> / {product.name}
      </nav>

      <div className="grid lg:grid-cols-[1.1fr_1fr] gap-10 lg:gap-16">
        {/* Gallery */}
        <div className="flex flex-col gap-4">
          <div className="relative aspect-square rounded-3xl bg-photo overflow-hidden">
            {image && <img src={image} alt={`${product.name} in ${color}`} className="h-full w-full object-cover" />}
            {product.is_new && <span className="absolute left-4 top-4"><Badge tone="accent">New</Badge></span>}
          </div>
          {product.colors.length > 1 && (
            <div className="grid grid-cols-4 sm:grid-cols-5 gap-3">
              {product.colors.map((c) => (
                <button key={c.name} onClick={() => { setColor(c.name); setAdded(false); }} aria-label={`Show ${c.name} photo`}
                  className={`aspect-square rounded-2xl bg-photo overflow-hidden border-2 ${c.name === color ? "border-ink" : "border-transparent hover:border-line-strong"}`}>
                  {c.image_url && <img src={c.image_url} alt="" className="h-full w-full object-cover" />}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* Details */}
        <div className="flex flex-col gap-6 lg:pt-2">
          <div className="flex flex-col gap-2">
            <p className="text-xs uppercase tracking-[0.16em] text-muted">{product.brand}</p>
            <h1 className="text-3xl sm:text-4xl font-semibold tracking-tight leading-tight">{product.name}</h1>
            <div className="flex items-center gap-4 mt-1">
              <span className="text-2xl font-semibold tabular-nums">{money(product.price)}</span>
              <Rating value={product.rating} count={product.review_count} size={15} />
            </div>
            {product.member && (
              <div className="flex flex-wrap items-center gap-2 mt-1">
                {product.member.offers.map((o) => (
                  <span key={o.offer_id} className="rounded-full bg-accent-soft text-accent px-2.5 py-1 text-xs font-semibold">
                    Chase card offer: {o.title}</span>
                ))}
                <span className="text-xs text-muted">
                  {product.member.offers.length
                    ? `Earn ${product.member.points.toLocaleString()} points when you pay with your Chase card · Chase offers apply at checkout`
                    : `Earn ${product.member.points.toLocaleString()} points`}</span>
              </div>
            )}
          </div>
          <p className="text-ink-soft leading-relaxed">{product.description}</p>

          <div className="flex flex-col gap-3">
            <p className="text-sm"><span className="text-muted">Colour:</span> <span className="font-medium">{color}</span></p>
            <div className="flex flex-wrap gap-1.5">
              {(avail?.colors ?? product.colors.map((c) => ({ ...c, available: c.in_stock }))).map((c) => (
                <Swatch key={c.name} label={c.name} hex={c.hex} selected={c.name === color} disabled={!c.available}
                  onClick={() => { setColor(c.name); setAdded(false); }} />
              ))}
            </div>
          </div>

          {hasSizes && (
            <div className="flex flex-col gap-3">
              <p className="text-sm"><span className="text-muted">{label}:</span> <span className="font-medium">{size ?? "Select"}</span></p>
              <div className="flex flex-wrap gap-2">
                {sizeChips.map((s) => (
                  <Chip key={s.size} selected={s.size === size} disabled={!s.available}
                    onClick={() => { setSize(s.size); setAdded(false); }}>{s.size}</Chip>
                ))}
              </div>
            </div>
          )}

          {selected && !selected.available && color && (!hasSizes || size) && (
            <Notice>{color}{size ? ` in ${label.toLowerCase()} ${size}` : ""} is out of stock. Try another {hasSizes ? `${label.toLowerCase()} or ` : ""}colour.</Notice>
          )}
          {selected?.available && selected.stock <= 5 && <p className="text-sm text-accent font-medium">Only {selected.stock} left</p>}

          <div className="flex flex-col sm:flex-row gap-3">
            {/* CH-2: the cart is open to everyone; signing in / up happens at checkout */}
            <Button size="lg" className="w-full sm:flex-1" onClick={add} loading={adding} disabled={!ready}>
              {added ? <><Check size={17} /> Added to cart</> : !color || (hasSizes && !size) ? `Select a ${hasSizes && !size ? label.toLowerCase() : "colour"}` : "Add to cart"}
            </Button>
            {TALKSHOP_ENABLED && (
              <Button size="lg" variant="talk" onClick={() => askTalkshop(product.product_id, product.name)}>
                <Sparkles size={16} /> Ask Talkshop about this
              </Button>
            )}
          </div>
          {added && <p className="text-sm text-good">Added to your bag. <Link to="/cart" className="underline font-medium">View cart</Link></p>}
          {error && <Notice>{error}</Notice>}

          <div className="rounded-2xl border border-line divide-y divide-line text-sm">
            <div className="flex items-center gap-3 px-4 py-3"><Truck size={17} className="text-muted" /> Free standard delivery by <span className="font-medium">{deliveryDate(product.delivery_days)}</span></div>
            <div className="flex items-center gap-3 px-4 py-3"><RotateCcw size={17} className="text-muted" /> 30-day free returns</div>
            <div className="flex items-center gap-3 px-4 py-3"><ShieldCheck size={17} className="text-muted" /> Secure checkout with saved cards</div>
          </div>

          {product.tags.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {product.tags.map((t) => <span key={t} className="rounded-full bg-panel px-3 py-1 text-xs text-ink-soft capitalize">{t}</span>)}
            </div>
          )}
        </div>
      </div>

      {related.length > 0 && (
        <section className="mt-20">
          <h2 className="text-2xl font-semibold tracking-tight mb-6">You may also like</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-5 gap-y-10">
            {related.map((p) => <ProductTile key={p.product_id} product={p} />)}
          </div>
        </section>
      )}
    </div>
  );
}
