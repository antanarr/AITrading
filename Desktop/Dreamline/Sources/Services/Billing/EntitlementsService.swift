import Foundation
import Observation

enum Tier: String, Codable, Hashable {
    case free, plus, pro
}

@Observable final class EntitlementsService {
    var tier: Tier = .free
    var lastRestoreMessage: String? = nil
    private var updateTask: Task<Void, Never>?
    
    private let tierKey = "dreamline_entitlement_tier"
    
    init() {
        // Load persisted tier
        if let saved = UserDefaults.standard.string(forKey: tierKey),
           let savedTier = Tier(rawValue: saved) {
            tier = savedTier
        }
    }
    
    private func saveTier(_ newTier: Tier) {
        tier = newTier
        UserDefaults.standard.set(newTier.rawValue, forKey: tierKey)
    }
    
    // Base stub methods - used when StoreKit is unavailable or fails
    func buyPlusStub() async {
        saveTier(.plus)
    }

    func buyProStub() async {
        saveTier(.pro)
    }

    func restoreStub() async {
        lastRestoreMessage = "Nothing to restore in stub mode."
    }
}

#if canImport(StoreKit)
import StoreKit

extension EntitlementsService {
    @available(iOS 15.0, *)
    func products() async -> [Product] {
        do {
            let ids = [IAPIDs.plus, IAPIDs.pro, IAPIDs.deep]
            return try await Product.products(for: ids)
        } catch {
            print("Failed to fetch products: \(error)")
            return []
        }
    }
    
    @available(iOS 15.0, *)
    func startObservers() async {
        // Cancel existing task if any
        updateTask?.cancel()
        
        updateTask = Task {
            for await result in Transaction.updates {
                do {
                    let transaction = try checkVerified(result)
                    await transaction.finish()
                    await updateTierFromTransactions()
                } catch {
                    print("Transaction verification failed: \(error)")
                }
            }
        }
    }
    
    @available(iOS 15.0, *)
    func currentEntitlements() async {
        await updateTierFromTransactions()
    }
    
    @available(iOS 15.0, *)
    private func updateTierFromTransactions() async {
        var currentTier: Tier = .free
        
        for await result in Transaction.currentEntitlements {
            do {
                let transaction = try checkVerified(result)
                
                // Check if it's a subscription (auto-renewable)
                if transaction.productType == .autoRenewable {
                    if transaction.productID == IAPIDs.pro {
                        currentTier = .pro
                        break // Pro is highest tier
                    } else if transaction.productID == IAPIDs.plus {
                        if currentTier != .pro {
                            currentTier = .plus
                        }
                    }
                }
            } catch {
                continue
            }
        }
        
        let finalTier = currentTier
        await MainActor.run {
            if finalTier != tier {
                saveTier(finalTier)
            }
        }
    }
    
    @available(iOS 15.0, *)
    private func checkVerified<T>(_ result: VerificationResult<T>) throws -> T {
        switch result {
        case .unverified(_, let error):
            throw error
        case .verified(let safe):
            return safe
        }
    }

    @available(iOS 15.0, *)
    func buyPlus() async {
        do {
            let products = try await Product.products(for: [IAPIDs.plus])
            guard let product = products.first else {
                // Fallback if product not available
                await buyPlusStub()
                return
            }
            
            let result = try await product.purchase()
            
            switch result {
            case .success(let verification):
                let transaction = try checkVerified(verification)
                await transaction.finish()
                await currentEntitlements() // Refresh tier
            case .userCancelled, .pending:
                break
            @unknown default:
                break
            }
        } catch {
            // Fallback on failure
            await buyPlusStub()
        }
    }

    @available(iOS 15.0, *)
    func buyPro() async {
        do {
            let products = try await Product.products(for: [IAPIDs.pro])
            guard let product = products.first else {
                // Fallback if product not available
                await buyProStub()
                return
            }
            
            let result = try await product.purchase()
            
            switch result {
            case .success(let verification):
                let transaction = try checkVerified(verification)
                await transaction.finish()
                await currentEntitlements() // Refresh tier
            case .userCancelled, .pending:
                break
            @unknown default:
                break
            }
        } catch {
            // Fallback on failure
            await buyProStub()
        }
    }
    
    @available(iOS 15.0, *)
    func buyDeepRead() async -> Bool {
        do {
            let products = try await Product.products(for: [IAPIDs.deep])
            guard let product = products.first else {
                return false
            }
            
            let result = try await product.purchase()
            
            switch result {
            case .success(let verification):
                let transaction = try checkVerified(verification)
                await transaction.finish()
                return true
            case .userCancelled, .pending:
                return false
            @unknown default:
                return false
            }
        } catch {
            print("Deep Read purchase failed: \(error)")
            return false
        }
    }

    @available(iOS 15.0, *)
    func restore() async {
        do {
            try await AppStore.sync()
            await currentEntitlements()
            await MainActor.run {
                lastRestoreMessage = "Purchases restored."
            }
        } catch {
            await MainActor.run {
                lastRestoreMessage = "Restore failed: \(error.localizedDescription)"
            }
            await restoreStub()
        }
    }
}
#endif

// Provide stub implementations when StoreKit is not available
#if !canImport(StoreKit)
extension EntitlementsService {
    func buyPlus() async {
        await buyPlusStub()
    }

    func buyPro() async {
        await buyProStub()
    }
    
    func buyDeepRead() async -> Bool {
        return false
    }

    func restore() async {
        await restoreStub()
    }
}
#endif
