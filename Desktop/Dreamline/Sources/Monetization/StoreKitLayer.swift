import Foundation

#if canImport(StoreKit)
import StoreKit

@MainActor
@available(iOS 15.0, *)
final class DreamlineStore: ObservableObject {
    static let shared = DreamlineStore()
    
    @Published private(set) var products: [Product] = []
    @Published private(set) var purchased: Set<String> = []
    @Published var isLoading = false
    
    // Product IDs
    let plusMonthly = "com.example.dreamline.plus.monthly"
    let proMonthly  = "com.example.dreamline.pro.monthly"
    let deepRead    = "com.example.dreamline.deepread.once"
    
    private init() { }
    
    func loadProducts() async {
        guard !isLoading else { return }
        isLoading = true
        defer { isLoading = false }
        
        do {
            let ids = [plusMonthly, proMonthly, deepRead]
            let products = try await Product.products(for: ids)
            self.products = products
            await updatePurchased()
        } catch { }
    }
    
    func purchase(_ product: Product) async -> Bool {
        do {
            let result = try await product.purchase()
            switch result {
            case .success(let verification):
                if case .verified(let transaction) = verification {
                    await transaction.finish()
                    await updatePurchased()
                    return true
                }
            default: break
            }
        } catch { }
        return false
    }
    
    func restore() async {
        try? await AppStore.sync()
        await updatePurchased()
    }
    
    private func updatePurchased() async {
        var active: Set<String> = []
        for await result in Transaction.currentEntitlements {
            if case .verified(let tx) = result {
                active.insert(tx.productID)
            }
        }
        purchased = active
    }
    
    func hasPlus() -> Bool { purchased.contains(plusMonthly) || hasPro() }
    func hasPro() -> Bool { purchased.contains(proMonthly) }
}

#else

@MainActor
final class DreamlineStore: ObservableObject {
    static let shared = DreamlineStore()
    
    @Published private(set) var products: [Any] = []
    @Published private(set) var purchased: Set<String> = []
    @Published var isLoading = false
    
    let plusMonthly = "com.example.dreamline.plus.monthly"
    let proMonthly  = "com.example.dreamline.pro.monthly"
    let deepRead    = "com.example.dreamline.deepread.once"
    
    private init() { }
    
    func loadProducts() async { }
    func purchase(_ product: Any) async -> Bool { return false }
    func restore() async { }
    func hasPlus() -> Bool { return false }
    func hasPro() -> Bool { return false }
}

#endif

